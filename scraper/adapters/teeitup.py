"""GolfNow TeeItUp adapter.

Booking pages: https://<alias>.book.teeitup.com/ (also .book.teeitup.golf,
<alias>.play.teeitup.com). All are the same SPA backed by a public JSON API:

    GET https://phx-api-be-east-1b.kenna.io/v2/tee-times?date=YYYY-MM-DD
        header: x-be-alias: <alias>

Optional params: facilityIds=<id>, and the same API serves course/facility
metadata at /v2/courses (with the alias header), which is how we discover
facility ids from just the alias.

Response shape (observed): a JSON array with one object per facility/day:
    [{"dayInfo": {...}, "teetimes": [
        {"teetime": "2026-07-24T13:30:00.000Z", "courseId": ..,
         "facilityId": .., "maxPlayers": 4, "minPlayers": 1,
         "rates": [{"greenFeeWalking": 6500, "greenFeeCart": 8500,
                     "holes": 18, "name": "..."}], ...}]}]
Prices are in cents.
"""
from __future__ import annotations

import datetime as dt
import threading
import time as _time
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://phx-api-be-east-1b.kenna.io"

# All ~22 TeeItUp courses hit this one host; unthrottled concurrency trips 429s.
# Cap concurrency and space requests so the shared host stays happy.
_KENNA_SEM = threading.Semaphore(2)      # ≤2 concurrent kenna.io requests
_KENNA_GAP = 0.35                        # min seconds between requests
_KENNA_LOCK = threading.Lock()
_KENNA_LAST = [0.0]


def _kenna_throttle():
    with _KENNA_LOCK:
        wait = _KENNA_GAP - (_time.monotonic() - _KENNA_LAST[0])
        if wait > 0:
            _time.sleep(wait)
        _KENNA_LAST[0] = _time.monotonic()


class TeeItUpAdapter(Adapter):
    platform = "teeitup"

    def _headers(self, alias: str) -> dict:
        return {"x-be-alias": alias}

    def discover_facilities(self, alias: str) -> list[dict]:
        """Return facility metadata (ids, names) for an alias."""
        data = self.get_json(f"{API_BASE}/v2/courses", headers=self._headers(alias))
        return data if isinstance(data, list) else data.get("courses", [])

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        alias = course["ids"].get("alias")
        if not alias:
            raise ValueError(f"{course['slug']}: missing TeeItUp alias "
                             "(booking URL did not yield one)")
        params: dict[str, Any] = {"date": date.isoformat()}
        if course["ids"].get("facility_id"):
            params["facilityIds"] = course["ids"]["facility_id"]

        with _KENNA_SEM:
            _kenna_throttle()
            data = self.get_json(f"{API_BASE}/v2/tee-times",
                                 headers=self._headers(alias), params=params)
        out: list[TeeTime] = []
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            for slot in block.get("teetimes", []):
                rates = slot.get("rates") or []
                cents = [r[k] for r in rates
                         for k in ("greenFeeWalking", "greenFeeCart")
                         if isinstance(r.get(k), (int, float))]
                holes = sorted({r.get("holes") for r in rates
                                if r.get("holes")})
                out.append(self.base_tee_time(
                    course,
                    teetime=slot.get("teetime", ""),  # UTC ISO; convert downstream
                    holes=[h for h in holes if h],
                    open_spots=slot.get("maxPlayers"),
                    price_min=min(cents) / 100 if cents else None,
                    price_max=max(cents) / 100 if cents else None,
                    raw=slot,
                ))
        return out
