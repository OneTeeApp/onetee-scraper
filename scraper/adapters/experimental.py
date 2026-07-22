"""Experimental adapters: MemberSports, Club Caddie, EZLinks/GolfNow, Teesnap.

These platforms either hide their JSON API behind SPA-set auth or actively
bot-protect. Each adapter documents the known entry point and makes a
best-effort attempt; failures come back as structured errors so the
aggregator can report coverage honestly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime


class MemberSportsAdapter(Adapter):
    """MemberSports (app.membersports.com).

    SPA calls api.membersports.com. Known pattern (subject to change):
        POST https://api.membersports.com/api/v1/golfclubs/{club_id}/teeTimes
             {"date": "YYYY-MM-DD", ...}
    The exact payload requires one-time capture from browser devtools
    (Network tab on app.membersports.com) — see ARCHITECTURE.md §ID discovery.
    """

    platform = "membersports"
    API = "https://api.membersports.com/api/v1/golfclubs/{club_id}/golfclubgroups/{secondary_id}/teeTimes/{date}/0"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        url = self.API.format(club_id=ids["club_id"],
                              secondary_id=ids["secondary_id"],
                              date=date.isoformat())
        data = self.get_json(url)
        out: list[TeeTime] = []
        for slot in data if isinstance(data, list) else []:
            t = slot.get("teeTime") or slot.get("time") or ""
            price = slot.get("price") or slot.get("greenFee")
            out.append(self.base_tee_time(
                course,
                teetime=t if "T" in str(t) else f"{date.isoformat()}T{t}",
                open_spots=slot.get("openSpots") or slot.get("available"),
                price_min=price, price_max=price,
                raw=slot,
            ))
        return out


class ClubCaddieAdapter(Adapter):
    """Club Caddie (apimanager-cc<NN>.clubcaddie.com/webapi/view/<token>).

    The public tee sheet is an SPA at /webapi/view/<token>/slots?date=...
    whose backing JSON endpoint needs devtools capture per shard. We attempt
    the documented slots URL and report what comes back.
    """

    platform = "clubcaddie"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        url = (f"https://apimanager-{ids['shard']}.clubcaddie.com/webapi/view/"
               f"{ids['view_token']}/slots")
        r = self.session.get(url, params={
            "date": date.strftime("%m/%d/%Y"), "player": "1", "ratetype": "any",
        }, timeout=20)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                "Club Caddie returned non-JSON (SPA shell) — JSON endpoint "
                "needs one-time devtools capture; see ARCHITECTURE.md")
        out: list[TeeTime] = []
        for slot in data if isinstance(data, list) else data.get("slots", []):
            out.append(self.base_tee_time(
                course, teetime=str(slot.get("time", "")), raw=slot))
        return out


class GolfNowAdapter(Adapter):
    """GolfNow / EZLinks.

    golfnow.com and *.ezlinksgolf.com are actively bot-protected; there is no
    stable anonymous JSON API. Production path = GolfNow affiliate/partner
    feed. This adapter exists so coverage reporting is explicit.
    """

    platform = "golfnow"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        raise RuntimeError(
            "GolfNow/EZLinks requires partner-feed access (bot-protected). "
            "Course visible at: " + course.get("booking_url", ""))


class OtherAdapter(Adapter):
    """Niche platforms (ForeTees, IBS Vision, SuperSaaS, Square Appointments).

    One or two CO courses each — not worth adapters yet. Coverage reporting
    stays explicit: these raise with the course's booking URL.
    """

    platform = "other"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        raise RuntimeError(
            f"platform {course['platform']} not implemented (niche, "
            f"{course['slug']} bookable at {course.get('booking_url','')})")
