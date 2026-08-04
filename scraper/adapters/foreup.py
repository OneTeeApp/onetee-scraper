"""ForeUp Software adapter.

Booking pages: https://foreupsoftware.com/index.php/booking/<course_id>/<schedule_id>#/teetimes
Tee-time API:  https://foreupsoftware.com/index.php/api/booking/times

Notes
-----
* The times endpoint is the same one the public booking page calls. It wants
  `schedule_id` and usually a `booking_class` (public-rate class id embedded in
  the booking page's JS config, e.g. in `bookingClasses` / `schedules` blobs).
* `api_key=no_limits` mirrors what the SPA sends for anonymous browsing.
* Some deployments answer without booking_class; we try without, then give a
  clear error so the ID-discovery pipeline knows to harvest it.
* discover_ids() pulls schedule_id / booking_class candidates out of the
  booking page HTML for courses where we only know the course_id.
* Respect robots/ToS: foreupsoftware.com disallows generic crawling of
  /index.php/*. Run this only at human-comparable rates for personal use, or
  with course/vendor permission for production (see ARCHITECTURE.md legal
  section).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import threading
import time
from typing import Any

from .base import Adapter, USER_AGENT
from ..models import TeeTime

API = "https://foreupsoftware.com/index.php/api/booking/times"
BOOKING_PAGE = "https://foreupsoftware.com/index.php/booking/{course_id}"

# Discovered ids (schedule_id / booking_class / teesheet_id) are STABLE per
# course, but foreup used to re-scrape the booking page for them on every fetch.
# The page load fails or serves a bot-check variant intermittently, so a course
# with no pinned schedule_id errored on random passes ("no schedule_id
# discoverable") and its near-date rows then went stale and hid — measured
# 2026-08-04 for patty-jewett / valley-hi (both discoverable, just flaky). We
# now cache the first successful discovery (memory + runner disk) and reuse it
# for the rest of the run, and retry the page fetch a few times before giving
# up. The disk cache is per-run (ephemeral runner) but the near loop runs many
# passes per run, so one good discovery covers the whole run.
_IDS_CACHE_PATH = pathlib.Path(".cache/foreup_ids.json")
_IDS_MEM: dict[str, dict] = {}
_IDS_LOADED = [False]
_IDS_LOCK = threading.Lock()


def _ids_cache_get(course_id: str) -> dict | None:
    with _IDS_LOCK:
        if not _IDS_LOADED[0]:
            try:
                _IDS_MEM.update(json.loads(_IDS_CACHE_PATH.read_text()))
            except Exception:  # noqa: BLE001 — cold cache is fine
                pass
            _IDS_LOADED[0] = True
        return _IDS_MEM.get(str(course_id))


def _ids_cache_put(course_id: str, ids: dict) -> None:
    with _IDS_LOCK:
        _IDS_MEM[str(course_id)] = ids
        try:
            _IDS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _IDS_CACHE_PATH.write_text(json.dumps(_IDS_MEM))
        except Exception:  # noqa: BLE001 — best-effort persistence
            pass


class ForeUpAdapter(Adapter):
    platform = "foreup"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        schedule_id = ids.get("schedule_id")
        if not schedule_id:
            # auto-discover from the booking page at runtime
            found = self.discover_ids(ids["course_id"])
            cands = found.get("schedule_id") or []
            if not cands:
                raise ValueError(
                    f"{course['slug']}: no schedule_id discoverable from "
                    f"booking page {ids['course_id']}")
            schedule_id = cands[0]
        params = {
            "time": "all",
            "date": date.strftime("%m-%d-%Y"),
            "holes": "all",
            "players": "0",
            "schedule_id": schedule_id,
            "schedule_ids[]": schedule_id,
            "specials_only": "0",
            "api_key": "no_limits",
        }
        if ids.get("booking_class"):
            params["booking_class"] = ids["booking_class"]

        data = self.get_json(
            API, params=params,
            headers={"x-fu-golfer-location": "foreup"},
        )
        out: list[TeeTime] = []
        for slot in data or []:
            # slot["time"] like "2026-07-24 07:30"
            t = slot.get("time", "").replace(" ", "T")
            prices = [v for v in (slot.get("green_fee"), slot.get("green_fee_18"),
                                  slot.get("green_fee_9")) if isinstance(v, (int, float))]
            out.append(self.base_tee_time(
                course,
                teetime=t,
                holes=[h for h in (9, 18) if slot.get(f"green_fee_{h}") is not None]
                      or ([slot["holes"]] if slot.get("holes") else []),
                open_spots=slot.get("available_spots"),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw=slot,
            ))
        return out

    # -- ID discovery --------------------------------------------------------

    ID_RES = {
        "schedule_id": re.compile(r'"schedule_id"\s*:\s*"?(\d+)'),
        "booking_class": re.compile(r'"booking_class_id"\s*:\s*"?(\d+)'),
        "teesheet_id": re.compile(r'"teesheet_id"\s*:\s*"?(\d+)'),
    }

    def discover_ids(self, course_id: str) -> dict[str, list[str]]:
        """Return schedule/booking-class ids for a course.

        Cache-first (memory + runner disk), then retry the booking-page fetch a
        few times — the page intermittently fails or serves a bot-check variant
        with no ids, which used to error the course on a random pass. A
        successful discovery is cached so the rest of the run never re-fetches.
        """
        cached = _ids_cache_get(course_id)
        if cached and cached.get("schedule_id"):
            return cached

        last: dict[str, list[str]] = {k: [] for k in self.ID_RES}
        for attempt in range(3):
            try:
                r = self.session.get(
                    BOOKING_PAGE.format(course_id=course_id), timeout=20,
                    headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                html = r.text
                found = {k: sorted(set(rx.findall(html)))
                         for k, rx in self.ID_RES.items()}
                if found.get("schedule_id"):
                    _ids_cache_put(course_id, found)
                    return found
                last = found            # 200 but no id: retry (bot-check variant)
            except Exception:           # noqa: BLE001 — transient page failure
                pass
            if attempt < 2:
                time.sleep(1.0 + attempt + 0.3 * attempt)
        return last
