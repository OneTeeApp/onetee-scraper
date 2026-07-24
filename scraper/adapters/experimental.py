"""Adapters captured from live traffic (July 2026): MemberSports, plus
best-effort GolfNow/EZLinks (bot-protected) and niche 'other' platforms.

Club Caddie, Teesnap, Club Prophet, Quick18, TeeItUp, ForeUp, Chronogolf each
live in their own module.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

from .base import Adapter
from ..models import TeeTime


class MemberSportsAdapter(Adapter):
    """MemberSports (app.membersports.com -> api.membersports.com).

    Implemented from a known-working reference scraper. The public tee-sheet is:

      POST https://api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes
        headers: x-api-key (platform key, same for all MemberSports courses),
                 Origin/Referer = app.membersports.com, browser User-Agent
        body:    {configurationTypeId:0, date:"YYYY-MM-DD", golfClubGroupId:0,
                  golfClubId:<int>, golfCourseId:<int>, groupSheetTypeId:0}
      -> JSON array of rows; each row = {teeTime:<minutes-since-midnight>,
         items:[{name, price, playerCount, golfCourseNumberOfHoles, teeTimeId,
                 bookingNotAllowed, hide}, ...]}.

    The x-api-key is a MemberSports platform identifier (sent by every client);
    it is provided/owned by the operator and overridable via MEMBERSPORTS_API_KEY.
    Registry ids: club_id -> golfClubId, secondary_id -> golfCourseId.
    """

    platform = "membersports"
    API = "https://api.membersports.com/api/v1"
    API_KEY = "A9814038-9E19-4683-B171-5A06B39147FC"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": os.environ.get("MEMBERSPORTS_API_KEY", self.API_KEY),
            "Origin": "https://app.membersports.com",
            "Referer": "https://app.membersports.com/",
        }

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        club_id, course_id = int(ids["club_id"]), int(ids["secondary_id"])
        body = {
            "configurationTypeId": 0,
            "date": date.isoformat(),
            "golfClubGroupId": 0,
            "golfClubId": club_id,
            "golfCourseId": course_id,
            "groupSheetTypeId": 0,
        }
        # post_json already retries on 5xx (this gateway intermittently 5xxs)
        data = self.post_json(f"{self.API}/golfclubs/onlineBookingTeeTimes",
                              json=body, headers=self._headers(), timeout=25)
        if not isinstance(data, list):
            raise RuntimeError(f"{course['slug']}: unexpected MemberSports "
                               f"response type {type(data).__name__}")

        # A club's response can span multiple sub-courses (Kennedy: 18-hole
        # sheets + "Kennedy Par 3 or Footgolf"), distinguished per item by
        # golfCourseId/name. Group items per (teeTime, golfCourseId) so each
        # sub-course keeps its own slot instead of maxing across them; label
        # only when the day actually spans more than one sub-course.
        course_ids_seen = {it.get("golfCourseId")
                           for row in data for it in row.get("items", [])
                           if it.get("golfCourseId") is not None}
        multi = len(course_ids_seen) > 1

        out: list[TeeTime] = []
        for row in data:
            tee_min = row.get("teeTime")
            if tee_min is None:
                continue
            groups: dict = {}
            for it in row.get("items", []):
                if it.get("bookingNotAllowed") or it.get("hide"):
                    continue
                spots = max(0, 4 - int(it.get("playerCount") or 0))
                if spots <= 0:
                    continue
                g = groups.setdefault(it.get("golfCourseId"), {
                    "prices": [], "spots": 0, "holes": set(), "name": ""})
                g["spots"] = max(g["spots"], spots)
                p = float(it.get("price") or 0)
                if p > 0:
                    g["prices"].append(p)
                if it.get("golfCourseNumberOfHoles"):
                    g["holes"].add(int(it["golfCourseNumberOfHoles"]))
                if not g["name"] and it.get("name"):
                    g["name"] = str(it["name"])
            hh, mm = divmod(int(tee_min), 60)
            for gid, g in groups.items():
                out.append(self.base_tee_time(
                    course,
                    teetime=f"{date.isoformat()}T{hh:02d}:{mm:02d}:00",
                    course_label=g["name"] if multi else "",
                    holes=sorted(g["holes"]),
                    open_spots=g["spots"],
                    price_min=min(g["prices"]) if g["prices"] else None,
                    price_max=max(g["prices"]) if g["prices"] else None,
                    raw={"teeTime": tee_min, "golfCourseId": gid},
                ))
        return out


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
