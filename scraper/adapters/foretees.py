"""ForeTees public tee-sheet adapter (web.foretees.com "foreteespublic" portal).

Some semi-private clubs (Dalton Ranch) expose a public availability portal —
a Flutter SPA whose backing API is plain, unauthenticated JSON (captured live
July 2026 from GitHub's runner):

  GET /v5/servlet/Public_verify_club?clubKey=<key>
    -> {"validClub": true}
  GET /v5/servlet/Public_teesheet?cid=<cid>&ckey=<key>&a=vts&d=YYYY-MM-DD
    -> {"foreTeesPublicTimesApiResp": {"data": [{
          "viewableDaysInAdvance": 7, "clubTimeZone": "America/Denver",
          "publicTimes": [{"date": "2026-07-23", "time": "17:50:00",
                           "nineHoles": 0, "openSlots": 4,
                           "greenFeeEighteen": 150.0, "greenFeeNine": 85.0,
                           "course": "", "courseId": null}, ...]}]}}

Booking happens on the same portal, so booking_url stays the registry URL.
Note viewableDaysInAdvance (7 for Dalton) — dates beyond it return empty,
which is correct behavior, not an error.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

BASE = "https://web.foretees.com/v5/servlet/Public_teesheet"


class ForeTeesAdapter(Adapter):
    platform = "foretees"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        key, cid = ids.get("club_key"), ids.get("cid")
        if not (key and cid):
            raise ValueError(f"{course['slug']}: foretees needs club_key + cid")
        data = self.get_json(BASE, params={
            "cid": cid, "ckey": key, "a": "vts", "d": date.isoformat()})
        blocks = ((data.get("foreTeesPublicTimesApiResp") or {}).get("data")) or []
        out: list[TeeTime] = []
        for block in blocks:
            for s in (block.get("publicTimes") or []):
                d, t = s.get("date"), s.get("time")
                if not (d and t):
                    continue
                nine = bool(s.get("nineHoles"))
                fee18 = s.get("greenFeeEighteen")
                fee9 = s.get("greenFeeNine")
                prices = [p for p in ((fee9,) if nine else (fee18, fee9))
                          if isinstance(p, (int, float)) and p > 0]
                spots = s.get("openSlots")
                out.append(self.base_tee_time(
                    course,
                    teetime=f"{d}T{t}",
                    holes=[9] if nine else ([9, 18] if fee9 else [18]),
                    open_spots=int(spots) if isinstance(spots, (int, float)) else None,
                    price_min=min(prices) if prices else None,
                    price_max=max(prices) if prices else None,
                    raw={},
                ))
        return out
