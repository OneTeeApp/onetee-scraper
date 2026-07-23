"""Club Caddie adapter — public tee-sheet, no secret required.

Captured from the live public booking page (Applewood, cc11/hbfdabab) July 2026.
The tee times are shown publicly (no login, no CAPTCHA) and the data request uses
the course's OWN published view token as the "apikey" — the same string that
appears in the booking URL the course hands out (apimanager-<shard>.clubcaddie.com
/webapi/view/<token>) and that we already store as ids.view_token. It is a public
identifier, not a credential.

Flow (plain HTTP, cookies via a normal session):

  1. GET https://apimanager-<shard>.clubcaddie.com/webapi/view/<token>
       -> establishes the session cookie; the page/config carries the CourseId.
  2. GET .../webapi/view/<token>/slots
         ?date=MM/DD/YYYY&player=1&holes=any&fromtime=5&totime=22
         &minprice=0&maxprice=9999&ratetype=any&HoleGroup=front
         &CourseId=<courseId>&apikey=<token>
       (with X-Requested-With: XMLHttpRequest) -> JSON tee-time slots.

NOTE: response field names below are best-effort from the rendered page
(time / price / holes / golfer-range / course name). The raw payload is kept on
every record, so the first live run surfaces the exact keys and we tighten the
parser from `runs.errors` + raw if anything is off.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

# CourseIds captured / researched per view token (fallback if page discovery fails)
KNOWN_COURSE_IDS = {
    "hbfdabab": "103418",   # Applewood
}

COURSEID_RE = re.compile(r'CourseId["\'=:\s]+(\d+)', re.I)


class ClubCaddieAdapter(Adapter):
    platform = "clubcaddie"

    def _base(self, shard: str) -> str:
        return f"https://apimanager-{shard}.clubcaddie.com"

    def _discover_course_id(self, base: str, token: str) -> str | None:
        html = self.session.get(f"{base}/webapi/view/{token}", timeout=20).text
        m = COURSEID_RE.search(html)
        return m.group(1) if m else None

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        shard = ids["shard"]            # e.g. "cc11"
        token = ids["view_token"]       # public view token from the booking URL
        base = self._base(shard)

        # establish a session (cookie) like any normal client loading the page
        self.session.get(f"{base}/webapi/view/{token}", timeout=20)

        course_id = (ids.get("clubcaddie_course_id")
                     or KNOWN_COURSE_IDS.get(token)
                     or self._discover_course_id(base, token))
        if not course_id:
            raise RuntimeError(f"{course['slug']}: could not resolve Club Caddie "
                               "CourseId from the booking page")

        params = {
            "date": date.strftime("%m/%d/%Y"),
            "player": 1, "holes": "any",
            "fromtime": 5, "totime": 22,
            "minprice": 0, "maxprice": 9999,
            "ratetype": "any", "HoleGroup": "front",
            "CourseId": course_id, "apikey": token,
        }
        headers = {"X-Requested-With": "XMLHttpRequest",
                   "Accept": "application/json, text/javascript, */*; q=0.01"}
        data = self.get_json(f"{base}/webapi/view/{token}/slots",
                             params=params, headers=headers)

        slots = self._slot_list(data)
        out: list[TeeTime] = []
        for slot in slots:
            out.append(self._parse(course, slot, date))
        return [t for t in out if t.teetime]

    # -- parsing (defensive; refine after first live run) --------------------

    @staticmethod
    def _slot_list(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("slots", "Slots", "data", "teeTimes", "TeeTimes", "times"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    def _parse(self, course, slot, date) -> TeeTime:
        g = lambda *keys: next((slot[k] for k in keys
                                if isinstance(slot, dict) and slot.get(k) not in
                                (None, "")), None)
        raw_time = g("Time", "time", "teeTime", "TeeTime", "startTime", "TeeTimeString")
        teetime = self._norm_time(raw_time, date)
        price = g("Rate", "rate", "Price", "price", "greenFee", "Amount")
        try:
            price = float(str(price).replace("$", "").replace(",", "")) if price is not None else None
        except ValueError:
            price = None
        holes_raw = g("Holes", "holes", "HoleCount", "numberOfHoles")
        holes = []
        if holes_raw:
            m = re.search(r"\d+", str(holes_raw))
            if m:
                holes = [int(m.group())]
        spots = g("MaxPlayers", "maxPlayers", "AvailableSpots", "availableSlots",
                  "openSpots", "Golfers")
        try:
            spots = int(re.search(r"\d+", str(spots)).group()) if spots else None
        except (ValueError, AttributeError):
            spots = None
        return self.base_tee_time(
            course,
            teetime=teetime or "",
            holes=holes,
            open_spots=spots,
            price_min=price, price_max=price,
            raw=slot if isinstance(slot, dict) else {"raw": str(slot)[:300]},
        )

    @staticmethod
    def _norm_time(raw, date) -> str:
        if not raw:
            return ""
        s = str(raw)
        if "T" in s and len(s) >= 16:          # already ISO
            return s
        # try "06:12 PM" style
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                t = dt.datetime.strptime(s.strip(), fmt).time()
                return dt.datetime.combine(date, t).isoformat()
            except ValueError:
                continue
        return ""
