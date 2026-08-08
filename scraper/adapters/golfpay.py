"""GolfPay adapter — golfpay.co plain JSON API. Anonymous: no login, no CAPTCHA.

Captured live 2026-08-08 (The Barn Golf Club, course_id 1466, tsid 20). One call:

    GET golfpay.co/api/tee-times
        ?date=MM/DD/YYYY&course_id=<cid>&tsid=<tsid>
        &source=&price_class_id=&number_of_holes=
    -> { data: { times: [ { local_tee_time: "2026-08-09 18:40:00",
                            number_of_holes: 18, min_allowed_golfers: 1,
                            max_allowed_golfers: 4, is_online_block: bool,
                            regular_golfer_price: "52.21",
                            booking_golfer_price: "52.21", ... }, ... ] } }

Registry ids: {"course_id": <int>, "tsid": <int>}. Neither is in the booking-page
URL (golfpay.co/course/<slug>); both are read off the page's own API calls, so a
URL-only row sits needs_ids.

FOUR HAZARDS, all measured on The Barn:

1. `is_online_block` MARKS AN OPERATIONAL BLOCK, NOT A BOOKABLE TEE TIME. 31 of
   57 rows on 08/09 were blocks (holes present, not sellable). Publishing them
   would advertise slots a golfer cannot book. Filtered out.

2. HTTP 422 IS "DATE OUT OF THE BOOKING WINDOW", NOT A FAULT. A past date
   (01/01/2020) and a far-future date (12/25/2026) both answered 422 with no
   times, while near dates serve 200. Left to raise_for_status this errors the
   course on every out-of-window date forever AND blocks `courses_empty` from
   deactivating stale rows. Translated to an empty list.

3. THE ONLINE PRICE, NOT THE RACK RATE. `regular_golfer_price` is the walk-up
   rack rate; `booking_golfer_price` is the price paid booking online (equal on
   The Barn — no online discount). We publish the online price as price_min and
   the rack as price_max, filtering to strictly-positive values (a $0/None field
   must never become the headline).

4. NO PER-SLOT COURSE ID TO SELF-ASSERT. Unlike teesnap/rguest the payload
   carries no courseId per row, so a wrong id cannot be caught from the response.
   `course_id` is a required query param that scopes the sheet and there is no
   shared-id ambiguity (a wrong id yields empty/422, not another course's sheet),
   so this is documented rather than guarded — the id is pinned, not derived.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

from .base import Adapter
from ..models import TeeTime

API = "https://golfpay.co/api/tee-times"


class GolfPayAdapter(Adapter):
    platform = "golfpay"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        cid, tsid = ids.get("course_id"), ids.get("tsid")
        if cid is None or tsid is None:
            raise ValueError(
                "golfpay: registry must pin ids.course_id and ids.tsid "
                "(read off golfpay.co/course/<slug>'s /api/tee-times call)")

        params = {
            "date": date.strftime("%m/%d/%Y"),
            "course_id": cid,
            "tsid": tsid,
            "source": "",
            "price_class_id": "",
            "number_of_holes": "",
        }
        try:
            data = self.get_json(API, params=params)
        except requests.HTTPError as e:
            if getattr(getattr(e, "response", None), "status_code", None) == 422:
                return []          # hazard 2: out-of-window date = empty sheet
            raise

        times = (((data or {}).get("data") or {}).get("times")) or []
        out: list[TeeTime] = []
        for s in times:
            if s.get("is_online_block"):        # hazard 1: block, not bookable
                continue
            tt = self._tee_time(course, s, date)
            if tt is not None:
                out.append(tt)
        return out

    def _tee_time(self, course: dict, s: dict, date: dt.date) -> TeeTime | None:
        teetime = self._iso(s.get("local_tee_time"))
        if teetime is None:
            return None
        lo, hi = self._prices(s)                 # hazard 3
        return self.base_tee_time(
            course,
            teetime=teetime,
            holes=self._holes(s.get("number_of_holes")),
            open_spots=self._spots(s),
            price_min=lo, price_max=hi,
            raw=s,
        )

    @staticmethod
    def _iso(value: str | None) -> str | None:
        """'2026-08-09 18:40:00' -> ISO. Already property-local wall clock."""
        if not value:
            return None
        try:
            return dt.datetime.fromisoformat(str(value)[:19]).isoformat(
                timespec="seconds")
        except ValueError:
            return None

    @staticmethod
    def _holes(v: Any) -> list[int]:
        try:
            h = int(v)
        except (TypeError, ValueError):
            return []
        return [h] if h in (9, 18) else []

    @staticmethod
    def _spots(s: dict) -> int | None:
        v = s.get("max_allowed_golfers")
        return int(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _prices(s: dict) -> tuple[float | None, float | None]:
        def num(key: str) -> float | None:
            try:
                v = float(s.get(key))
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None
        online = num("booking_golfer_price")
        rack = num("regular_golfer_price")
        vals = [p for p in (online, rack) if p is not None]
        if not vals:
            return None, None
        return min(vals), max(vals)
