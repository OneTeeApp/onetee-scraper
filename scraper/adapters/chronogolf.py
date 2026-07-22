"""Chronogolf (Lightspeed Golf) adapter — fully API-driven, no browser needed.

Discovery chain (all plain HTTP, verified July 2026 via live capture):

  1. GET /private_api/clubs/<slug or club_id>
       -> { id, uuid, settings.default_affiliation_type_id }
  2. GET /private_api/clubs/<club_id>/courses
       -> [ { id, name, online_booking_enabled }, ... ]
  3. GET /marketplace/clubs/<club_id>/teetimes
         ?date=YYYY-MM-DD
         &course_id=<course_id>
         &affiliation_type_ids[]=<aff>&affiliation_type_ids[]=<aff>   (one per player)
         &nb_holes=18
       -> [ { start_time, date, hole, out_of_capacity,
              green_fees:[{ green_fee, affiliation_type_id, ... }] }, ... ]

The affiliation_type_id ("Public" green-fee category) is the crux — it lives at
settings.default_affiliation_type_id and MUST be supplied or the API 422s with
"Player type provided is not valid". We send it twice (2-player pricing) to get
a representative rate; open capacity is read from out_of_capacity.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

BASE = "https://www.chronogolf.com"


class ChronogolfAdapter(Adapter):
    platform = "chronogolf"

    # -- discovery -----------------------------------------------------------

    def _club(self, slug_or_id: str) -> dict:
        return self.get_json(f"{BASE}/private_api/clubs/{slug_or_id}")

    def _courses(self, club_id: int) -> list[dict]:
        data = self.get_json(f"{BASE}/private_api/clubs/{club_id}/courses")
        return data if isinstance(data, list) else data.get("courses", [])

    def discover(self, slug_or_id: str) -> dict:
        """Resolve everything the fetch needs from a slug or numeric club id."""
        club = self._club(slug_or_id)
        club_id = club["id"]
        aff = (club.get("settings") or {}).get("default_affiliation_type_id")
        courses = [c for c in self._courses(club_id)
                   if c.get("online_booking_enabled")]
        return {"club_id": club_id, "affiliation_type_id": aff,
                "course_ids": [c["id"] for c in courses],
                "course_names": {c["id"]: c.get("name", "") for c in courses}}

    # -- fetch ---------------------------------------------------------------

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        key = ids.get("club_id") or ids.get("slug")
        if not key:
            raise ValueError(f"{course['slug']}: no chronogolf slug/club_id")

        disc = self.discover(str(key))
        aff = disc["affiliation_type_id"]
        if not aff:
            raise RuntimeError(f"{course['slug']}: no default_affiliation_type_id "
                               "(club may be contact-only / no online booking)")
        course_ids = ids.get("course_ids") or disc["course_ids"]
        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no online-bookable courses")

        out: list[TeeTime] = []
        for cid in course_ids:
            params = [
                ("date", date.isoformat()),
                ("course_id", cid),
                ("affiliation_type_ids[]", aff),
                ("affiliation_type_ids[]", aff),
                ("nb_holes", 18),
            ]
            slots = self.get_json(f"{BASE}/marketplace/clubs/{disc['club_id']}/teetimes",
                                 params=params)
            cname = disc["course_names"].get(cid, course["name"])
            for slot in slots or []:
                if slot.get("out_of_capacity"):
                    continue
                fees = [f.get("green_fee") for f in slot.get("green_fees", [])
                        if isinstance(f.get("green_fee"), (int, float))]
                start = slot.get("start_time", "")
                out.append(self.base_tee_time(
                    course,
                    teetime=f"{slot.get('date', date.isoformat())}T{start}"
                            + ("" if len(start) > 5 else ":00"),
                    holes=[18],
                    open_spots=slot.get("open_slots") or (
                        None if slot.get("out_of_capacity") else 4),
                    price_min=min(fees) if fees else None,
                    price_max=max(fees) if fees else None,
                    raw={"course_name": cname, **{k: slot.get(k) for k in
                         ("start_time", "hole", "out_of_capacity")}},
                ))
        return out
