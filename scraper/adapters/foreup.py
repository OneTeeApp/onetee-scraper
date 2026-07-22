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
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

API = "https://foreupsoftware.com/index.php/api/booking/times"
BOOKING_PAGE = "https://foreupsoftware.com/index.php/booking/{course_id}"


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
        """Fetch the booking page and regex out schedule/booking-class ids."""
        r = self.session.get(BOOKING_PAGE.format(course_id=course_id), timeout=20)
        r.raise_for_status()
        html = r.text
        return {k: sorted(set(rx.findall(html))) for k, rx in self.ID_RES.items()}
