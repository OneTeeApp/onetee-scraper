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

    THE TWO PARAMETERS THAT ACTUALLY MATTER (live-probed 2026-07-25, see
    probe-results/diag2.txt) — both were previously pinned to 0, which silently
    lost most of a multi-course club's inventory:

      golfCourseId = 0   means "every course this club owns". Pinning it to one
        id returns only that sheet, so `course_label` never had more than one
        value to distinguish and sub-course names came back blank.

      configurationTypeId  selects WHICH tee sheet, and a club's sheets are not
        all reachable from one value. Kennedy (club 3629) is the clear case:
          cfg 0 -> "Kennedy Par 3 or Footgolf"
          cfg 1 -> "Kennedy (Babe Lind / Creek)"
          cfg 2 -> "Kennedy (West 9 only)"
        With cfg pinned to 0, Kennedy's two 18-hole configurations were
        invisible. Foothills (club 3697) returns all three of its sheets at
        cfg 0, so the right behaviour is to sweep the values a course declares.

      golfClubGroupId MUST stay 0. With 1 the API ignores golfClubId entirely
        and returns the whole linked-club group (all eight Denver municipals),
        which is how four courses ended up serving a neighbour's tee sheet.

    Registry ids:
      club_id       -> golfClubId (required)
      config_ids    -> configurationTypeIds to sweep; defaults to [0]
      course_ids    -> OPTIONAL allow-list of golfCourseIds. Omit it and every
                       course the club returns is kept, which is correct for
                       the normal case where one club == one venue and its
                       extra ids are that venue's own sheets. Pin it only where
                       two of our venues share a golfClubId, so neither serves
                       the other's tee sheet.
      secondary_id  -> the golfCourseId from the booking URL; informational
                       only now that the request always asks for id 0.
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

    def _sheet(self, club_id: int, cfg: int, date: dt.date) -> list:
        """One tee-sheet POST. golfCourseId=0 => every course this club owns."""
        body = {
            "configurationTypeId": cfg,
            "date": date.isoformat(),
            "golfClubGroupId": 0,   # 1 would ignore golfClubId — see docstring
            "golfClubId": club_id,
            "golfCourseId": 0,
            "groupSheetTypeId": 0,
        }
        # post_json already retries on 5xx (this gateway intermittently 5xxs)
        data = self.post_json(f"{self.API}/golfclubs/onlineBookingTeeTimes",
                              json=body, headers=self._headers(), timeout=25)
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected MemberSports response type "
                               f"{type(data).__name__} (cfg {cfg})")
        return data

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        club_id = int(ids["club_id"])
        # `want` is None = keep every course the club returns, which is the
        # right default: a club normally IS one venue, and its extra ids are
        # that venue's own sheets (Fox Hollow's three nines, Foothills' Par 3 /
        # Executive 9 / Back Nine). Pin course_ids only where two of OUR venues
        # genuinely share one golfClubId, so neither serves the other's times.
        want = ({int(x) for x in ids["course_ids"]}
                if ids.get("course_ids") else None)
        cfgs = [int(x) for x in (ids.get("config_ids") or [0])]

        # Sweep the configurations this venue declares. A club's tee sheets are
        # not all reachable from one configurationTypeId, and the same
        # golfCourseId can appear under different sheet names across cfgs
        # (Kennedy 20573 = "Babe Lind / Creek" at cfg 1, "West 9 only" at 2),
        # so the sub-course key is (golfCourseId, item name), not the id alone.
        errors: list[str] = []
        # (teeTime, courseId, name) -> aggregate
        groups: dict[tuple, dict] = {}
        for cfg in cfgs:
            try:
                data = self._sheet(club_id, cfg, date)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"cfg {cfg}: {type(exc).__name__}: {exc}")
                continue
            for row in data:
                tee_min = row.get("teeTime")
                if tee_min is None:
                    continue
                for it in row.get("items", []):
                    cid = it.get("golfCourseId")
                    if cid is None or (want is not None and int(cid) not in want):
                        continue
                    if it.get("bookingNotAllowed") or it.get("hide"):
                        continue
                    spots = max(0, 4 - int(it.get("playerCount") or 0))
                    if spots <= 0:
                        continue
                    name = str(it.get("name") or "").strip()
                    g = groups.setdefault((int(tee_min), int(cid), name), {
                        "prices": [], "spots": 0, "holes": set()})
                    g["spots"] = max(g["spots"], spots)
                    p = float(it.get("price") or 0)
                    if p > 0:
                        g["prices"].append(p)
                    if it.get("golfCourseNumberOfHoles"):
                        g["holes"].add(int(it["golfCourseNumberOfHoles"]))

        # Every configuration errored and nothing came back: that is a failure,
        # not an empty day. A partial sweep still yields what it found.
        if not groups and errors and len(errors) == len(cfgs):
            raise RuntimeError(f"{course['slug']}: all MemberSports "
                               f"configurations failed — " + "; ".join(errors))

        # Label only when the day actually spans more than one sub-course.
        names = {k[2] for k in groups}
        multi = len(names) > 1

        out: list[TeeTime] = []
        for (tee_min, cid, name), g in groups.items():
            hh, mm = divmod(tee_min, 60)
            out.append(self.base_tee_time(
                course,
                teetime=f"{date.isoformat()}T{hh:02d}:{mm:02d}:00",
                course_label=name if multi else "",
                holes=sorted(g["holes"]),
                open_spots=g["spots"],
                price_min=min(g["prices"]) if g["prices"] else None,
                price_max=max(g["prices"]) if g["prices"] else None,
                raw={"teeTime": tee_min, "golfCourseId": cid},
            ))
        out.sort(key=lambda t: (t.teetime, t.course_label))
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
