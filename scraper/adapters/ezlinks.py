"""EZLinks adapter (ezlinksgolf.com portals).

Captured live July 2026 from cityofaurora.ezlinksgolf.com. Behind Cloudflare,
but the challenge clears for a normal client; the JSON API is otherwise open:

  1. GET  https://<portal>.ezlinksgolf.com/api/search/init
       -> { AllCourseIDs: "6386,6474,...", Courses:[{CourseID,CourseName}],
            StartTime:"05:00:00", EndTime:"19:00:00" }
  2. POST https://<portal>.ezlinksgolf.com/api/search/search
       body: {p01:[courseIds], p02:"MM/DD/YYYY", p03:"5:00 AM", p04:"7:00 PM",
              p05:0, p06:2, p07:false}
       -> { r06: [ {r15:ISO time, r16:course name, r07:courseId,
                    r25:price, r06:rateCategoryId, r14:maxPlayers}, ... ] }

One portal covers several registry courses, so we fetch the whole portal once
per (portal, date) and cache it, then each registry course filters to its own
slots by course-name match (Back-9 variants -> 9-hole slots on the same course).
"""
from __future__ import annotations

import datetime as dt
import re
import threading
from typing import Any

from .base import Adapter, USER_AGENT
from ..models import TeeTime

_CACHE: dict[tuple[str, str], list[dict]] = {}
_LOCK = threading.Lock()


def _norm(name: str) -> str:
    """Normalize a course name to its distinctive tokens for matching."""
    n = name.lower()
    n = re.sub(r"~?\s*back\s*9", "", n)
    n = re.sub(r"golf course|golf club|country club|\bthe\b|\bat\b|\bof\b", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return " ".join(n.split())


class EZLinksAdapter(Adapter):
    platform = "ezlinks"

    def _headers(self, portal: str) -> dict:
        base = f"https://{portal}.ezlinksgolf.com"
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": base, "Referer": base + "/index.html"}

    def _load_portal(self, portal: str, date: dt.date) -> list[dict]:
        key = (portal, date.isoformat())
        with _LOCK:
            if key in _CACHE:
                return _CACHE[key]
        base = f"https://{portal}.ezlinksgolf.com"
        h = self._headers(portal)
        init = self.get_json(f"{base}/api/search/init", headers=h)
        ids = [int(x) for x in str(init.get("AllCourseIDs", "")).split(",")
               if x.strip().isdigit()]
        if not ids:
            raise RuntimeError(f"EZLinks {portal}: no course ids from init "
                               "(Cloudflare challenge or empty portal)")
        body = {"p01": ids, "p02": date.strftime("%m/%d/%Y"),
                "p03": "5:00 AM", "p04": "7:00 PM", "p05": 0, "p06": 2,
                "p07": False}
        resp = self.post_json(f"{base}/api/search/search", json=body, headers=h)
        slots = self.raw_to_slots(resp.get("r06") or [])
        with _LOCK:
            _CACHE[key] = slots
        return slots

    @staticmethod
    def raw_to_slots(r06: list[dict]) -> list[dict]:
        """Map the API's r06 rows to portal slot dicts. Shared with the
        browser fetcher, which runs the same init+search call in-page."""
        slots = []
        for t in (r06 or []):
            slots.append({
                "time": t.get("r15"), "name": t.get("r16", "") or "",
                "course_id": t.get("r07"),
                "price": float(t.get("r25") or 0),
                "max_players": t.get("r14"),
            })
        return slots

    @classmethod
    def course_teetimes(cls, course: dict[str, Any], slots: list[dict]) -> list[TeeTime]:
        """Filter a portal's slots to one registry course, folding same-time
        18/9-hole variants together. Shared by the plain and browser fetchers."""
        want = _norm(course["name"])
        by_time: dict[str, dict] = {}
        for s in slots:
            nm = s["name"]
            base_nm = _norm(nm)
            # match this registry course to the slot's course name
            if not (want and (want in base_nm or base_nm in want)):
                continue
            is9 = bool(re.search(r"back\s*9|9\s*hole", nm.lower()))
            e = by_time.setdefault(s["time"], {"prices": [], "holes": set(),
                                               "spots": 0})
            if s["price"] > 0:
                e["prices"].append(s["price"])
            e["holes"].add(9 if is9 else 18)
            if s.get("max_players"):
                e["spots"] = max(e["spots"], int(s["max_players"]))

        out: list[TeeTime] = []
        for tt, e in by_time.items():
            if not tt:
                continue
            out.append(cls.base_tee_time(
                course,
                teetime=str(tt),
                holes=sorted(e["holes"]),
                open_spots=e["spots"] or None,
                price_min=min(e["prices"]) if e["prices"] else None,
                price_max=max(e["prices"]) if e["prices"] else None,
                raw={},
            ))
        return out

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        portal = course["ids"].get("portal")
        if not portal:
            raise ValueError(f"{course['slug']}: no EZLinks portal")
        slots = self._load_portal(portal, date)
        return self.course_teetimes(course, slots)
