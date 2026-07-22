"""Teesnap adapter (best-effort).

Teesnap booking sites (https://<sub>.teesnap.net) are React SPAs backed by a
same-origin customer API. Known endpoint family (may vary by deployment):

    GET https://<sub>.teesnap.net/customer-api/branches
        → branches[].courses[] with course ids
    GET https://<sub>.teesnap.net/customer-api/tee-times-page/tee-times
        ?date=YYYY-MM-DD&courseIds=<id>&players=0&holes=0

We try the documented shapes and raise a descriptive error if the deployment
differs — one devtools capture on any Teesnap site is enough to finalize.
Used in CO by: Cattails Alamosa, Heather Gardens, Mt Massive, Pagosa Springs,
Hollydot, Coyote Creek, Petteys Park.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime


class TeesnapAdapter(Adapter):
    platform = "teesnap"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        sub = course["ids"]["subdomain"]
        base = f"https://{sub}.teesnap.net"

        course_ids = course["ids"].get("teesnap_course_ids")
        if not course_ids:
            branches = self.get_json(f"{base}/customer-api/branches")
            course_ids = ",".join(
                str(c.get("id"))
                for b in (branches if isinstance(branches, list)
                          else branches.get("branches", []))
                for c in (b.get("courses") or []))
        if not course_ids:
            raise RuntimeError("Teesnap: no course ids discoverable — "
                               "capture the SPA's API calls once via devtools")

        data = self.get_json(
            f"{base}/customer-api/tee-times-page/tee-times",
            params={"date": date.isoformat(), "courseIds": course_ids,
                    "players": 0, "holes": 0})
        slots = data if isinstance(data, list) else data.get("teeTimes") or []
        out: list[TeeTime] = []
        for slot in slots:
            t = slot.get("teeTime") or slot.get("time") or ""
            prices = [v for k in ("greenFee", "price", "rate")
                      if isinstance((v := slot.get(k)), (int, float))]
            out.append(self.base_tee_time(
                course,
                teetime=str(t).replace(" ", "T"),
                holes=[slot["holes"]] if slot.get("holes") else [],
                open_spots=slot.get("availableSpots") or slot.get("openSlots"),
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw=slot,
            ))
        return out
