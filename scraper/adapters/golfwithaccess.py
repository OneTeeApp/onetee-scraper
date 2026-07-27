"""Golf With Access adapter — Troon's public booking platform (golfwithaccess.com).

Captured from live traffic (July 2026), no auth, no CAPTCHA for viewing tee
times (Cloudflare Turnstile gates the booking step only).

Fetch (plain HTTP, anonymous):

    GET https://golfwithaccess.com/api/v1/tee-times
        ?courseIds=<uuid>&players=0
        &startAt=00:00:00&endAt=23:59:59&day=YYYY-MM-DD
    -> { teeTimes: [ { dayTime:{year,month,day,hour,minute,second},
                       players:{min,max}, holesOption:"EIGHTEEN"|"NINE",
                       price:{min:{cents},max:{cents}},
                       rates:[{name:"Public", price:{dollars:{cents}}, ...}],
                       course:{id,slug,name}, facility:{...} }, ... ] }

THREE HAZARDS THAT SHAPE THIS FILE, all measured (browser network capture):

1. `courseIds` is per-COURSE, and only ONE id at a time returns rows. Passing
   the facility id, a back-nine id, or two ids comma-joined all return an empty
   list, not an error. So the registry pins the exact bookable course uuid per
   venue (EXTRA_IDS in build_registry.py), captured once from each tenant page's
   SSR `courses:[{id,name}]` array.

2. The id named after the facility is a DEAD AGGREGATE. El Conquistador's
   "El Conquistador Golf Club" id returns 0 while its "Conquistador Course" id
   returns ~55/day; Starr Pass's headline id returns 0 while "Gambler/Pioneer"
   serves. Tucson City Golf (the shared muni tenant) returns 0 on its own id but
   each of the five munis serves on its own. The pinned id is always the one
   proven to return rows, never the facility-named one.

3. A wrong id does not fail loudly — it succeeds with SOME course's sheet. So
   every returned slot is checked against the pinned id: `course.id` must match,
   or the slot is dropped and a mismatch is raised rather than published under
   our name. Same discipline as teesnap's global-id hazard.

Shared tenants (Tucson City Golf, El Conquistador) are multiple registry
venues that each pin their own course uuid, so they populate independently and
carry their own city/name — no sub-course labelling needed here, unlike the
membersports/teeitup multi-sheet venues.

Price: golfers who are not Access members pay the "Public" rate. price.max is
the rack/public figure and price.min is the Access-discounted one, so we
publish the Public rate explicitly (falling back to price.max) — never the
member discount, which a walk-up cannot get.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

API = "https://golfwithaccess.com/api/v1/tee-times"
BOOKING_PAGE = "https://golfwithaccess.com/course/{tenant}/reserve-tee-time"


class GolfWithAccessAdapter(Adapter):
    platform = "golfwithaccess"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        course_id = ids.get("course_id")
        if not course_id:
            raise ValueError("golfwithaccess: no course_id pinned in registry "
                             "(capture the uuid from the tenant page's courses[] "
                             "array — see EXTRA_IDS)")

        data = self.get_json(API, params={
            "courseIds": course_id,
            "players": 0,                      # 0 = every party size the sheet has
            "startAt": "00:00:00",
            "endAt": "23:59:59",
            "day": date.isoformat(),
        })
        slots = (data or {}).get("teeTimes") or []

        tenant = ids.get("tenant") or ""
        booking_url = (course.get("booking_url")
                       or (BOOKING_PAGE.format(tenant=tenant) if tenant else ""))

        out: list[TeeTime] = []
        for s in slots:
            # Hazard 3: never publish another course's sheet under our name.
            sc = (s.get("course") or {})
            if sc.get("id") and sc["id"] != course_id:
                raise ValueError(
                    f"golfwithaccess: pinned id {course_id} returned a slot for "
                    f"{sc.get('id')} ({sc.get('name')}) — refusing to publish it")

            teetime = self._iso(s.get("dayTime"))
            if teetime is None:
                continue

            holes = self._holes(s.get("holesOption"))
            players = s.get("players") or {}
            open_spots = players.get("max")
            lo, hi = self._public_price(s)

            tt = self.base_tee_time(
                course, teetime=teetime, holes=holes,
                open_spots=open_spots, price_min=lo, price_max=hi,
                raw=s,
            )
            # base_tee_time seeds booking_url from the registry row; deepen it
            # to the chosen date so a golfer lands on the right day's sheet.
            tt.booking_url = self._slot_url(booking_url, date)
            out.append(tt)
        return out

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _iso(dtm: dict | None) -> str | None:
        if not dtm:
            return None
        try:
            return dt.datetime(
                int(dtm["year"]), int(dtm["month"]), int(dtm["day"]),
                int(dtm.get("hour", 0)), int(dtm.get("minute", 0)),
                int(dtm.get("second", 0)),
            ).isoformat(timespec="seconds")
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _holes(opt: str | None) -> list[int]:
        return {"EIGHTEEN": [18], "NINE": [9]}.get(opt or "", [])

    @staticmethod
    def _public_price(slot: dict) -> tuple[float | None, float | None]:
        """The rate a non-member actually pays. Prefer the explicit Public rate;
        fall back to the slot's max (rack) price, never the Access min."""
        cents: list[int] = []
        for r in (slot.get("rates") or []):
            if r.get("name") == "Public":
                d = ((r.get("price") or {}).get("dollars") or {})
                if d.get("cents") is not None:
                    cents.append(int(d["cents"]))
        if cents:
            return min(cents) / 100.0, max(cents) / 100.0
        price = slot.get("price") or {}
        mx = (price.get("max") or {}).get("cents")
        mn = (price.get("min") or {}).get("cents")
        # public = the higher (undiscounted) figure; use it for both bounds so
        # we never advertise a member-only price to a walk-up.
        pub = mx if mx is not None else mn
        return (pub / 100.0 if pub is not None else None,
                pub / 100.0 if pub is not None else None)

    @staticmethod
    def _slot_url(base: str, date: dt.date) -> str:
        if not base:
            return ""
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}day={date.isoformat()}"
