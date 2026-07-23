"""Adapters captured from live traffic (July 2026): MemberSports, plus
best-effort GolfNow/EZLinks (bot-protected) and niche 'other' platforms.

Club Caddie, Teesnap, Club Prophet, Quick18, TeeItUp, ForeUp, Chronogolf each
live in their own module.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime


class MemberSportsAdapter(Adapter):
    """MemberSports (app.membersports.com -> api.membersports.com).

    Captured from the live booking app (Coal Creek portal 3663/4714):

      tee-time search  POST https://api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes
        body: {golfClubId, golfCourseId, golfCourseTypeId:0,
               beginDate, endDate, memberProfileId, profileId:0,
               numberOfPlayers:1, numberOfHoles:0}
      no auth header required for the public flow.

    memberProfileId is the club's default online-booking profile; we read it
    from the teesheetparameters endpoint. Courses are enumerated from
    /golfclubs/<club>/coursesslim so one portal (e.g. Denver's 3660/4711)
    expands to all its courses.
    """

    platform = "membersports"
    API = "https://api.membersports.com/api/v1"

    def _default_profile_id(self, club_id: str, course_id: str) -> int:
        try:
            p = self.get_json(
                f"{self.API}/golfclubs/{club_id}/golfCourses/{course_id}"
                f"/types/0/teesheetparameters")
            for k in ("memberProfileId", "defaultProfileId", "profileId"):
                if isinstance(p, dict) and p.get(k):
                    return int(p[k])
        except Exception:
            pass
        return 0  # 0 = public/default; server accepts it

    def _courses(self, club_id: str) -> list[dict]:
        try:
            data = self.get_json(f"{self.API}/golfclubs/{club_id}/coursesslim")
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
        return []

    def _warm(self, club_id: str) -> None:
        """The booking app loads several club endpoints before its POST; hitting
        one warms the session (any anonymous cookie) the way a browser would."""
        try:
            self.session.get(f"{self.API}/golfclubs/{club_id}/name", timeout=15)
        except Exception:
            pass

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        club_id = ids["club_id"]
        self._warm(club_id)
        # a portal may cover several courses; use the registry's course if the
        # secondary id is a real course, else enumerate.
        courses = self._courses(club_id)
        targets = [(c.get("golfCourseId") or c.get("id"),
                    c.get("golfCourseName") or c.get("name") or course["name"])
                   for c in courses] or [(ids["secondary_id"], course["name"])]

        out: list[TeeTime] = []
        for course_id, cname in targets:
            profile = self._default_profile_id(club_id, course_id)
            body = {
                "golfClubId": int(club_id), "golfCourseId": int(course_id),
                "golfCourseTypeId": 0,
                "beginDate": date.isoformat(), "endDate": date.isoformat(),
                "memberProfileId": profile, "profileId": 0,
                "numberOfPlayers": 1, "numberOfHoles": 0,
            }
            # retrying POST: this gateway intermittently 504s even for payloads
            # that succeed moments later.
            data = self.post_json(f"{self.API}/golfclubs/onlineBookingTeeTimes",
                                  json=body, timeout=30)
            slots = data if isinstance(data, list) else data.get("teeTimes", [])
            for slot in slots:
                out.extend(self._parse_slot(course, cname, slot, date))
        return out

    def _parse_slot(self, course, cname, slot, date) -> list[TeeTime]:
        # each slot may carry a list of rates / fees
        t = slot.get("teeTime") or slot.get("time") or slot.get("teeTimeDateTime")
        if t and "T" not in str(t):
            t = f"{date.isoformat()}T{t}"
        fees = []
        for k in ("greenFeeAmount", "price", "rate", "memberRate", "publicRate"):
            v = slot.get(k)
            if isinstance(v, (int, float)):
                fees.append(float(v))
        for rate in slot.get("rates", []) or slot.get("feeList", []) or []:
            v = rate.get("amount") or rate.get("price") if isinstance(rate, dict) else None
            if isinstance(v, (int, float)):
                fees.append(float(v))
        spots = (slot.get("availableSpots") or slot.get("openSlots")
                 or slot.get("maxPlayers"))
        return [self.base_tee_time(
            course,
            teetime=str(t or ""),
            holes=[slot["holes"]] if slot.get("holes") else [],
            open_spots=spots,
            price_min=min(fees) if fees else None,
            price_max=max(fees) if fees else None,
            raw={"course_name": cname},
        )]


class GolfNowAdapter(Adapter):
    """GolfNow / EZLinks — bot-protected; no stable anonymous JSON API.
    Production path = GolfNow affiliate/partner feed. Explicit so coverage
    reporting stays honest (user opted to circle back on these)."""

    platform = "golfnow"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        raise RuntimeError(
            "GolfNow/EZLinks needs partner-feed access (bot-protected). "
            "Course visible at: " + course.get("booking_url", ""))


class OtherAdapter(Adapter):
    """Niche platforms (ForeTees, IBS Vision, SuperSaaS, Square) — 1-2 courses
    each; not yet implemented. Raises with the booking URL for visibility."""

    platform = "other"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        raise RuntimeError(
            f"platform {course['platform']} not implemented (niche, "
            f"{course['slug']} bookable at {course.get('booking_url','')})")
