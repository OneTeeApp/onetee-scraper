"""Club Prophet (CPS Golf) adapter.

Tenant booking sites: https://<tenant>.cps.golf/onlineresweb/search-teetime
Underlying API (v1 online reservation):

    GET https://<tenant>.cps.golf/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes
        ?searchDate=YYYY-MM-DD&holes=18&numberOfPlayer=0
        &courseIds=&searchTimeType=0&teeOffTimeMin=0&teeOffTimeMax=23

Some tenants require SPA-set headers (x-websitename / x-apikey / x-componentid)
that are visible in the page's runtime config; we send sensible defaults and
surface the HTTP error when a tenant wants more. Response (observed shape):
list of {"teeOffTime"/"startTime", "availablePlayer", "shItemPrices"/"price"...}
— field names vary slightly by CPS version, so parsing is defensive.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

API = ("https://{tenant}.cps.golf/onlineres/onlineapi/api/v1/"
       "onlinereservation/TeeTimes")


class ClubProphetAdapter(Adapter):
    platform = "clubprophet"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        tenant = course["ids"]["tenant"]
        params = {
            "searchDate": date.isoformat(),
            "holes": 18,
            "numberOfPlayer": 0,
            "courseIds": course["ids"].get("cps_course_ids", ""),
            "searchTimeType": 0,
            "teeOffTimeMin": 0,
            "teeOffTimeMax": 23,
            "isChangeTeeOffTime": "true",
        }
        headers = {
            "x-websitename": tenant,
            "x-componentid": "1",
        }
        data = self.get_json(API.format(tenant=tenant), params=params,
                             headers=headers)
        slots = data if isinstance(data, list) else data.get("data") or []
        out: list[TeeTime] = []
        for slot in slots:
            t = (slot.get("teeOffTime") or slot.get("startTime")
                 or slot.get("teeTime") or "")
            prices = [p for p in self._extract_prices(slot)]
            out.append(self.base_tee_time(
                course,
                teetime=t.replace(" ", "T"),
                holes=[slot.get("holes")] if slot.get("holes") else [18],
                open_spots=(slot.get("availablePlayer")
                            or slot.get("availableSpots")),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw=slot,
            ))
        return out

    @staticmethod
    def _extract_prices(slot: dict) -> list[float]:
        prices: list[float] = []
        for key in ("price", "greenFee", "webPrice"):
            v = slot.get(key)
            if isinstance(v, (int, float)):
                prices.append(float(v))
        for item in slot.get("shItemPrices") or slot.get("prices") or []:
            v = item.get("price") if isinstance(item, dict) else None
            if isinstance(v, (int, float)):
                prices.append(float(v))
        return prices
