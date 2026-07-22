"""Chronogolf (Lightspeed Golf) adapter.

Club pages: https://www.chronogolf.com/club/<slug>
Public marketplace API:

    GET https://www.chronogolf.com/marketplace/clubs/<CLUB_UUID>/teetimes
        ?date=YYYY-MM-DD&course_ids=<id>&nb_holes=18&affiliation_type_ids=<ids>

* CLUB_UUID + numeric course ids + affiliation_type_ids (rate categories,
  repeat the id once per player) are embedded in the club page's JS state.
  discover_ids() regexes them out of the raw club-page HTML.
* Response: JSON array of slots with "start_time", "out_of_capacity",
  "green_fees": [{"green_fee": 45.0, ...}].
* Production alternative: the official Lightspeed Golf Partner API
  (partner-api.docs.chronogolf.com) — OAuth, sanctioned, the right path
  once you have volume.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

API = "https://www.chronogolf.com/marketplace/clubs/{uuid}/teetimes"
CLUB_PAGE = "https://www.chronogolf.com/club/{slug}"

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
COURSE_ID_RE = re.compile(r'"course_ids?"\s*:\s*\[?([\d,\s]+)')


class ChronogolfAdapter(Adapter):
    platform = "chronogolf"

    def discover_ids(self, slug: str) -> dict[str, Any]:
        """Regex club uuid / course ids out of the club page HTML."""
        r = self.session.get(CLUB_PAGE.format(slug=slug), timeout=20)
        r.raise_for_status()
        html = r.text
        uuids = sorted(set(UUID_RE.findall(html)))
        course_ids = sorted({cid.strip() for m in COURSE_ID_RE.findall(html)
                             for cid in m.split(",") if cid.strip().isdigit()})
        return {"club_uuid_candidates": uuids, "course_id_candidates": course_ids}

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        if not ids.get("club_uuid"):
            raise ValueError(
                f"{course['slug']}: missing club_uuid — run discover_ids() first")
        params: dict[str, Any] = {"date": date.isoformat(), "nb_holes": 18}
        if ids.get("course_ids"):
            params["course_ids"] = ids["course_ids"]
        if ids.get("affiliation_type_ids"):
            # one id per player being priced; 1 player is enough to get a rate
            params["affiliation_type_ids"] = ids["affiliation_type_ids"]

        data = self.get_json(API.format(uuid=ids["club_uuid"]), params=params)
        out: list[TeeTime] = []
        for slot in data or []:
            if slot.get("out_of_capacity"):
                continue
            fees = [f.get("green_fee") for f in slot.get("green_fees", [])
                    if isinstance(f.get("green_fee"), (int, float))]
            out.append(self.base_tee_time(
                course,
                teetime=f"{date.isoformat()}T{slot.get('start_time', '')}",
                holes=[params["nb_holes"]],
                open_spots=slot.get("free_slots") or (
                    4 - len(slot.get("green_fees", [])) if slot.get("green_fees") else None),
                price_min=min(fees) if fees else None,
                price_max=max(fees) if fees else None,
                raw=slot,
            ))
        return out
