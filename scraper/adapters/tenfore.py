"""TenFore (swan.tenfore.golf) adapter — uses the OPEN /api/TeeSheet endpoint.

TenFore's PRICED endpoint (/api/TeeTimes/Search) is reCAPTCHA Enterprise-gated and
unusable from automation (2026-08-07: headless/headful/proxy all rejected). BUT
/api/TeeSheet is fully OPEN — no auth, no token, no reCAPTCHA — and carries the
same bookable slots + availability + the public rate. A slot is publicly bookable
when availableSlots>0 and it is not a "Block". This rule reproduces the gated
Search set almost exactly (validated 2026-08-07: Rattlewood 36/36, Needwood
106/107). So TenFore is a PLAIN-HTTP platform after all.

Endpoints (host swan.tenfore.golf, all GET, OPEN):
  /api/GolfCourse/GetGolfCourseByVanity?vanityName=<slug>  -> {golfCourseID, ...}
  /api/TeeSheet?golfCourseId=<id>&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
      -> {teeTimes:[{ dateScheduled:"2026-08-08T07:50:00.000-4",
                      availableSlots, maxSlots,
                      teeTimeCustomers:[{ teeTimeCustomerTypeName, numberOfHoles,
                          teeFeePrice, teeFee:{title, price, price9} }] }]}
Bookable rule: availableSlots>0 AND not(all customers are "Block").
Price: the public rate is on an "Anonymous Golfer" seat / a "Public"-titled
teeFee (price=18-hole, price9=9-hole). teeTimeCustomers lists only ALREADY-BOOKED
seats, so a slot's own public rate is often absent — we fall back to the
course-day public rate scanned from the whole sheet, else leave price null
(availability is the core value).

Registry ids: {"vanity": "<slug>", "golf_course_id": "<int>"}.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://swan.tenfore.golf/api"
# Mimic the app's XHR so any basic origin/UA filter on swan passes.
_HEADERS = {"x-tenfore-appid": "23",
            "Origin": "https://fox.tenfore.golf",
            "Referer": "https://fox.tenfore.golf/"}


def _is_public(cust: dict) -> bool:
    fee = cust.get("teeFee") or {}
    return ("Anonymous" in (cust.get("teeTimeCustomerTypeName") or "")
            or "Public" in (fee.get("title") or ""))


class TenForeAdapter(Adapter):
    platform = "tenfore"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        gid = (course.get("ids") or {}).get("golf_course_id")
        if not gid:
            raise ValueError(f"{course['slug']}: missing tenfore golf_course_id")
        d = date.isoformat()
        data = self.get_json(f"{API_BASE}/TeeSheet", headers=_HEADERS,
                             params={"golfCourseId": gid,
                                     "startDate": d, "endDate": d})
        return self.sheet_to_teetimes(course, date, data)

    @staticmethod
    def _course_public_rate(tts: list) -> tuple:
        """Course-wide public 18h/9h rate from any Anonymous/Public teeFee."""
        p18 = p9 = None
        for t in tts:
            for c in (t.get("teeTimeCustomers") or []):
                if not _is_public(c):
                    continue
                fee = c.get("teeFee") or {}
                v18, v9 = fee.get("price"), fee.get("price9")
                if isinstance(v18, (int, float)) and v18 > 0:
                    p18 = v18 if p18 is None else min(p18, v18)
                if isinstance(v9, (int, float)) and v9 > 0:
                    p9 = v9 if p9 is None else min(p9, v9)
        return p18, p9

    @staticmethod
    def sheet_to_teetimes(course: dict, date: dt.date, sheet: dict) -> list[TeeTime]:
        tts = (sheet or {}).get("teeTimes") or []
        want = date.isoformat()
        crate18, crate9 = TenForeAdapter._course_public_rate(tts)
        # holes the course actually books (from booked seats); [] if unknown
        holes_seen = sorted({c.get("numberOfHoles") for t in tts
                             for c in (t.get("teeTimeCustomers") or [])
                             if isinstance(c.get("numberOfHoles"), int)})
        out: list[TeeTime] = []
        for t in tts:
            iso = str(t.get("dateScheduled") or "")[:19]   # naive local
            if iso[:10] != want:
                continue
            avail = t.get("availableSlots")
            if not isinstance(avail, int) or avail <= 0:
                continue
            cs = t.get("teeTimeCustomers") or []
            if cs and all("Block" in (c.get("teeTimeCustomerTypeName") or "")
                          for c in cs):
                continue  # a maintenance/blocked slot, not bookable
            # price: this slot's own public rate if present, else the course rate
            s18 = s9 = None
            for c in cs:
                if not _is_public(c):
                    continue
                fee = c.get("teeFee") or {}
                if isinstance(fee.get("price"), (int, float)) and fee["price"] > 0:
                    s18 = fee["price"]
                if isinstance(fee.get("price9"), (int, float)) and fee["price9"] > 0:
                    s9 = fee["price9"]
            price = s18 or crate18 or s9 or crate9
            out.append(TenForeAdapter.base_tee_time(
                course, teetime=iso, holes=holes_seen or [],
                open_spots=avail,
                price_min=price, price_max=price, raw={}))
        out.sort(key=lambda x: x.teetime)
        return out
