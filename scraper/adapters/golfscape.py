"""Golfscape adapter — golfscape.com consumer marketplace, plain JSON API.

Anonymous: no login, no session bootstrap, no CAPTCHA. One POST per date, the
same call the course page's booking box fires on a date change (captured live
2026-08-10 on Copper Rock Golf Course, propertyId 3713):

    POST golfscape.com/executeaction
        body (form-urlencoded): data=<urlencoded JSON>
            {"action":"booking-box-fetch-teetimes",
             "propertyId":"<id>","selectedDate":"YYYY-MM-DD"}
    -> {"data":{"error":false,"teetimeData":[
          {"name":"18 Holes","items":[
            {"id":"125052832","dateandtime":"2026-08-14 08:36:00","time":"8:36",
             "meridiem":"AM","available":4,"rate":130,"currency":"$",
             "rateId":"21664","allowedPlayers":[1,2,3,4]}, ...]}]}}

Registry id: {"property_id": <int-as-string>}. golfscape's numeric propertyId is
NOT in any public URL — the course page uses a NAME slug
(golfscape.com/<region>/<slug>) and the embeddable widget uses an opaque
courseCode (e.g. 125e71). The number is only read off the booking box's own
`booking-box-fetch-teetimes` request, so a URL-only row sits needs_ids until it
is pinned (mirrors golfpay/rguest).

Notes / hazards:
- A `bookingSessionId` is sent by the live page but is OPTIONAL: the call returns
  the identical sheet without it (verified on two dates), so no session bootstrap
  is needed.
- An out-of-window / closed date returns 200 with `error:false` and an empty
  items list — a trustworthy-empty sheet, NOT an HTTP fault. So there is no
  special status to translate: empty == empty.
- `rate` is the single online price per slot (min == max). Filtered to > 0 so a
  0/None never becomes the headline price.
- `holes` come from the GROUP `name` ("18 Holes" / "9 Holes"), not per item.
- `available` is the count of open spots. `allowedPlayers` is a party-size
  whitelist, not an availability figure — ignored.
- No per-slot propertyId echoes back (like golfpay), so a wrong id cannot be
  caught from the response — but a wrong id yields empty/!error, never another
  course's sheet, so the id is pinned rather than guarded.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import time
from typing import Any

import requests

from .base import MAX_RETRIES, RETRY_STATUS, TIMEOUT, Adapter
from ..models import TeeTime

API = "https://golfscape.com/executeaction"


class GolfscapeAdapter(Adapter):
    platform = "golfscape"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        pid = ids.get("property_id")
        if not pid:
            raise ValueError(
                "golfscape: registry must pin ids.property_id (golfscape's "
                "numeric propertyId, read off the course page's "
                "booking-box-fetch-teetimes call)")

        payload = {
            "action": "booking-box-fetch-teetimes",
            "propertyId": str(pid),
            "selectedDate": date.strftime("%Y-%m-%d"),
        }
        data = self._post_action(payload)

        blob = (data or {}).get("data") or {}
        if blob.get("error"):
            # golfscape signals a real fault in-band; treat as unknown (raise)
            # so sync does not deactivate the sheet's existing rows.
            raise requests.HTTPError(f"golfscape error for property {pid}")

        out: list[TeeTime] = []
        for group in blob.get("teetimeData") or []:
            holes = self._holes(group.get("name"))
            for s in group.get("items") or []:
                tt = self._tee_time(course, s, holes)
                if tt is not None:
                    out.append(tt)
        return out

    def _post_action(self, payload: dict) -> Any:
        """POST the form-encoded `data=<json>` body with polite retry."""
        body = {"data": json.dumps(payload, separators=(",", ":"))}
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = self.session.post(API, data=body, headers=headers,
                                      timeout=TIMEOUT)
                if r.status_code in RETRY_STATUS:
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                r.raise_for_status()
                return r.json()
            except (requests.HTTPError, requests.ConnectionError,
                    requests.Timeout, ValueError) as e:
                last_exc = e
                status = getattr(getattr(e, "response", None),
                                 "status_code", None)
                if status is not None and status not in RETRY_STATUS:
                    raise
                if attempt < MAX_RETRIES:
                    time.sleep((1.6 ** attempt) + random.uniform(0, 0.6))
        raise last_exc

    def _tee_time(self, course: dict, s: dict, holes: list[int]) -> TeeTime | None:
        teetime = self._iso(s.get("dateandtime"))
        if teetime is None:
            return None
        avail = s.get("available")
        if isinstance(avail, (int, float)) and avail <= 0:
            return None          # 0 open spots = not bookable, don't advertise
        price = self._num(s.get("rate"))
        return self.base_tee_time(
            course,
            teetime=teetime,
            holes=holes,
            open_spots=self._spots(s.get("available")),
            price_min=price, price_max=price,
            raw=s,
        )

    @staticmethod
    def _iso(value: str | None) -> str | None:
        """'2026-08-14 08:36:00' -> ISO. Already property-local wall clock."""
        if not value:
            return None
        try:
            return dt.datetime.fromisoformat(str(value)[:19]).isoformat(
                timespec="seconds")
        except ValueError:
            return None

    @staticmethod
    def _holes(name: str | None) -> list[int]:
        text = str(name or "")
        found = [h for h in (9, 18) if f"{h}" in text]
        return found or []

    @staticmethod
    def _spots(v: Any) -> int | None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    @staticmethod
    def _num(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None
