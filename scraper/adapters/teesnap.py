"""Teesnap adapter — captured from live traffic (July 2026), no auth, no CAPTCHA
for viewing tee times (reCAPTCHA only gates the actual booking step).

Discovery + fetch (both plain HTTP):

  1. GET https://<sub>.teesnap.net/         (the homepage shell)
       inlines `window.courses = [{ id, key, name, min_players, max_players,
       holes, enabled }, ...]` — regex out the course id(s).
  2. GET https://<sub>.teesnap.net/customer-api/teetimes-day
         ?course=<id>&date=YYYY-MM-DD&players=1&holes=18&addons=off
       -> { teeTimes: { teeTimes: [ { prices:[{roundType, price}],
                                      teeOffSections:[{turnTo:{time}}] } ],
                        bookings: [...] } }

Prices are strings ("55.00"); times are ISO ("2026-07-24T09:40:00").
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

COURSES_RE = re.compile(r"window\.courses\s*=\s*(\[.*?\]);", re.S)


class TeesnapAdapter(Adapter):
    platform = "teesnap"

    def discover_courses(self, sub: str) -> list[dict]:
        """Regex the inlined window.courses array out of the homepage."""
        html = self.session.get(f"https://{sub}.teesnap.net/", timeout=20).text
        m = COURSES_RE.search(html)
        if not m:
            # fallback: first "id": N near a "key" field
            ids = re.findall(r'"id":\s*(\d+),[^{}]*"key"', html)
            return [{"id": int(i)} for i in ids]
        try:
            arr = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        return [c for c in arr if c.get("enabled", True)]

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        sub = course["ids"]["subdomain"]
        course_ids = course["ids"].get("teesnap_course_ids")
        names: dict[int, str] = {}
        if not course_ids:
            discovered = self.discover_courses(sub)
            course_ids = [c["id"] for c in discovered]
            names = {c["id"]: c.get("name", course["name"]) for c in discovered}
        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no Teesnap course id in "
                               "window.courses")

        out: list[TeeTime] = []
        for cid in course_ids:
            data = self.get_json(
                f"https://{sub}.teesnap.net/customer-api/teetimes-day",
                params={"course": cid, "date": date.isoformat(),
                        "players": 1, "holes": 18, "addons": "off"})
            block = (data or {}).get("teeTimes", {})
            for slot in block.get("teeTimes", []):
                prices = [float(p["price"]) for p in slot.get("prices", [])
                          if p.get("price") not in (None, "")]
                holes = sorted({18 if p.get("roundType") == "EIGHTEEN_HOLE"
                                else 9 for p in slot.get("prices", [])})
                for sec in slot.get("teeOffSections", []) or [{}]:
                    t = (sec.get("turnTo") or {}).get("time") or sec.get("time")
                    if not t:
                        continue
                    out.append(self.base_tee_time(
                        course,
                        teetime=str(t),
                        holes=holes,
                        open_spots=None,  # derive from bookings if needed later
                        price_min=min(prices) if prices else None,
                        price_max=max(prices) if prices else None,
                        raw={"course_name": names.get(cid, course["name"])},
                    ))
        return out
