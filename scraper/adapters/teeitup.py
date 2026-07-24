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
import os
import threading
import time as _time
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://phx-api-be-east-1b.kenna.io"

# All TeeItUp courses hit one shared kenna host, so the WHOLE fleet — across
# every parallel shard — must stay under its burst 429 limit. Within a process
# we cap concurrency and space requests; across S shards we widen the per-shard
# gap to GAP*S so the aggregate cadence is constant no matter how many shards
# run (SHARD_COUNT is published by scraper.sharding). This is what lets TeeItUp
# scale to thousands of courses without one shard's pace multiplying by S.
_KENNA_SEM = threading.Semaphore(2)      # <=2 concurrent kenna.io reqs per shard
_KENNA_BASE_GAP = 0.7                     # global min seconds between requests
_KENNA_LOCK = threading.Lock()
_KENNA_LAST = [0.0]


def _kenna_gap() -> float:
    try:
        shards = max(1, int(os.environ.get("SHARD_COUNT", "1")))
    except ValueError:
        shards = 1
    return _KENNA_BASE_GAP * shards


def _kenna_throttle():
    with _KENNA_LOCK:
        wait = _kenna_gap() - (_time.monotonic() - _KENNA_LAST[0])
        if wait > 0:
            _time.sleep(wait)
        _KENNA_LAST[0] = _time.monotonic()


class TeeItUpAdapter(Adapter):
    platform = "teeitup"

    def _headers(self, alias: str) -> dict:
        return {"x-be-alias": alias}

    def discover_facilities(self, alias: str) -> list[dict]:
        """Return facility metadata (ids, names) for an alias.

        Most aliases answer /v2/courses, but some (Granby Ranch) 404 there
        while still resolving on the older /alias/<alias>/facilities route,
        so fall back to it."""
        try:
            data = self.get_json(f"{API_BASE}/v2/courses",
                                 headers=self._headers(alias))
        except Exception:  # noqa: BLE001 — some aliases 404 on /v2/courses
            data = self.get_json(f"{API_BASE}/alias/{alias}/facilities",
                                 headers=self._headers(alias))
        return data if isinstance(data, list) else data.get("courses", [])

    def _teetimes(self, alias: str, date: dt.date, facility_ids=None):
        params: dict[str, Any] = {"date": date.isoformat()}
        if facility_ids:
            params["facilityIds"] = facility_ids
        with _KENNA_SEM:
            _kenna_throttle()
            return self.get_json(f"{API_BASE}/v2/tee-times",
                                 headers=self._headers(alias), params=params)

    def _facility_ids(self, alias: str) -> str:
        """Resolve this alias's facility/course ids via /v2/courses."""
        with _KENNA_SEM:
            _kenna_throttle()
            data = self.discover_facilities(alias)
        ids = []
        for c in (data if isinstance(data, list) else []):
            fid = c.get("id") or c.get("facilityId") or c.get("courseId")
            if fid:
                ids.append(str(fid))
        return ",".join(ids)

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        alias = course["ids"].get("alias")
        if not alias:
            raise ValueError(f"{course['slug']}: missing TeeItUp alias "
                             "(booking URL did not yield one)")
        facility_id = course["ids"].get("facility_id")
        try:
            data = self._teetimes(alias, date, facility_id)
        except Exception:
            # Some courses 404/500 on the bare call (or a stale facility_id);
            # discover the real facility ids from /v2/courses and retry.
            fids = self._facility_ids(alias)
            if not fids:
                raise
            data = self._teetimes(alias, date, fids)

        blocks = data if isinstance(data, list) else [data]
        # if the bare call returned nothing, try explicit facility ids once
        if not any(b.get("teetimes") for b in blocks) and not facility_id:
            fids = self._facility_ids(alias)
            if fids:
                data = self._teetimes(alias, date, fids)
                blocks = data if isinstance(data, list) else [data]

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
