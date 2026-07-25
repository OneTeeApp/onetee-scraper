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
import zoneinfo
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://phx-api-be-east-1b.kenna.io"

# kenna's `teetime` strings are true UTC ("2026-07-25T18:40:00.000Z" = 12:40 PM
# Denver — probe-verified). They MUST be converted to course-local time or the
# site shows times ~6-7h in the future (which also read as bookable-past slots).
# Facility metadata carries the IANA timeZone; fall back by state.
_STATE_TZ = {"CO": "America/Denver", "AZ": "America/Phoenix"}

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

    # alias -> {courseId: {"name": str, "tz": str}} — one kenna call per alias
    # per process, shared across threads.
    _META: dict[str, dict] = {}
    _META_LOCK = threading.Lock()
    # alias -> raw /v2/courses payload, so the ids and the labels come from ONE
    # call. kenna 429s the whole fleet off one host, so every saved request
    # counts (probe-results/verify_fixes.txt section A shows live 429s).
    _FACILITIES: dict[str, list] = {}

    def _headers(self, alias: str) -> dict:
        return {"x-be-alias": alias}

    def _facilities_cached(self, alias: str) -> list[dict]:
        """discover_facilities() with a per-alias cache. Raises on failure."""
        with self._META_LOCK:
            if alias in self._FACILITIES:
                return self._FACILITIES[alias]
        with _KENNA_SEM:
            _kenna_throttle()
            facilities = self.discover_facilities(alias)
        facilities = facilities if isinstance(facilities, list) else []
        with self._META_LOCK:
            self._FACILITIES[alias] = facilities
        return facilities

    def _facility_meta(self, alias: str) -> dict:
        """courseId -> {name, tz} for an alias (cached; empty dict on failure)."""
        with self._META_LOCK:
            if alias in self._META:
                return self._META[alias]
        meta: dict = {}
        try:
            for f in self._facilities_cached(alias):
                cid = f.get("courseId") or f.get("id")
                if cid is not None:
                    meta[str(cid)] = {"name": f.get("name") or "",
                                      "tz": f.get("timeZone") or ""}
        except Exception:  # noqa: BLE001 — fall back to state tz, no labels
            pass
        with self._META_LOCK:
            self._META[alias] = meta
        return meta

    @staticmethod
    def _to_local(utc_iso: str, tz_name: str) -> str:
        """'2026-07-25T18:40:00.000Z' + America/Denver -> '2026-07-25T12:40:00'."""
        try:
            t = dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
            if t.tzinfo is None:
                return utc_iso                    # already naive/local: keep
            local = t.astimezone(zoneinfo.ZoneInfo(tz_name))
            return local.replace(tzinfo=None).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001 — malformed input: keep raw
            return utc_iso

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
        ids = []
        for c in self._facilities_cached(alias):
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

        # facilityIds is REQUIRED, not optional. Measured live on 10 pinned
        # courses x 3 dates (probe-results/verify_fixes.txt section A): the
        # call WITH the pinned id returns the real sheet — aguila 56/61/61,
        # cave-creek 54/73/68, encanto-18 63/87/83, las-sendas 51/62/62,
        # ak-chin 41/0/47 — while the bare per-alias call on the same alias
        # and date returns a dayInfo with an EMPTY teetimes list every time.
        # 869 slots vs 0.
        #
        # a248c79 removed the param on the strength of one diag4 sample where
        # every alias 500'd. That sample was transient: kenna's gateway
        # intermittently 5xxs and 429s (this run caught live 429s too), which
        # is exactly why get_json already retries 5xx. One bad sample is not
        # an API change.
        #
        # Order: the pinned id first (it is the right answer for a shared
        # alias), then the ids discovered from /v2/courses, then the bare call
        # as a last resort. Retry only on FAILURE — an empty list is a real
        # empty day (granby-ranch, rollingstone-ranch) and must report zero
        # rather than raise, which was the other half of the old bug.
        errors: list[Exception] = []

        def try_call(fids: str | None):
            """-> kenna's response, or None if this parameter shape failed."""
            try:
                return self._teetimes(alias, date, fids)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return None

        data = try_call(str(facility_id)) if facility_id else None
        if data is None:
            discovered = ""
            try:
                discovered = self._facility_ids(alias)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            if discovered and discovered != str(facility_id or ""):
                data = try_call(discovered)
        if data is None:
            data = try_call(None)
        if data is None:
            raise errors[0]

        blocks = data if isinstance(data, list) else [data]
        meta = self._facility_meta(alias)
        state_tz = _STATE_TZ.get(course.get("state", ""), "America/Denver")

        # A pinned facility_id means this registry row is ONE course inside a
        # shared alias (four Phoenix munis share city-of-phoenix-golf-courses),
        # so anything belonging to a sibling must be dropped or we would
        # publish another course's tee sheet under this name.
        want = {p.strip() for p in str(facility_id).split(",")
                if p.strip()} if facility_id else set()
        slots = []
        for block in blocks:
            for slot in (block or {}).get("teetimes", []) or []:
                if want and not ({str(slot.get("facilityId")),
                                  str(slot.get("courseId"))} & want):
                    continue
                slots.append(slot)

        # label slots per sub-course only when this response actually spans
        # multiple courses (multi-course facility like Hyland Hills)
        seen_cids = {str(s.get("courseId")) for s in slots if s.get("courseId")}
        multi = len(seen_cids) > 1

        out: list[TeeTime] = []
        for slot in slots:
            rates = slot.get("rates") or []
            cents = [r[k] for r in rates
                     for k in ("greenFeeWalking", "greenFeeCart")
                     if isinstance(r.get(k), (int, float))]
            holes = sorted({r.get("holes") for r in rates
                            if r.get("holes")})
            fmeta = meta.get(str(slot.get("courseId")), {})
            out.append(self.base_tee_time(
                course,
                teetime=self._to_local(slot.get("teetime", ""),
                                       fmeta.get("tz") or state_tz),
                course_label=(fmeta.get("name") or "") if multi else "",
                holes=[h for h in holes if h],
                # maxPlayers reflects how many can still book (probe: with
                # bookedPlayers=2 it reads 2, i.e. remaining seats)
                open_spots=slot.get("maxPlayers"),
                price_min=min(cents) / 100 if cents else None,
                price_max=max(cents) / 100 if cents else None,
                raw=slot,
            ))
        return out
