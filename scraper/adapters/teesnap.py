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

    def _get_text(self, url: str) -> str:
        """GET page text with retry — Teesnap intermittently resets the
        connection (ConnectionResetError 104) from datacenter IPs; a retry
        almost always succeeds on the next attempt."""
        import time
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
                return r.text
            except Exception as e:  # noqa: BLE001 — connection resets included
                last = e
                if attempt < 3:
                    time.sleep(1.0 + attempt)
        raise last  # type: ignore[misc]

    def discover_courses(self, sub: str) -> list[dict]:
        """Extract course ids from the homepage-inlined `window.courses` data.

        Robust to nested arrays in course objects (which broke the old
        non-greedy array regex). Each course object reliably starts
        `"id":<n>,"created_at"`, so we anchor on that inside the window.courses
        region and de-dupe. Disabled courses simply return empty tee-times, so
        over-including is harmless.
        """
        html = self._get_text(f"https://{sub}.teesnap.net/")
        start = html.find("window.courses")
        region = html[start:start + 30000] if start >= 0 else html
        ids: list[str] = []
        for i in re.findall(r'"id":\s*(\d+)\s*,\s*"created_at"', region):
            if i not in ids:
                ids.append(i)
        if not ids:  # last-ditch fallback
            for i in re.findall(r'"id":\s*(\d+),[^{}]*"key"', html):
                if i not in ids:
                    ids.append(i)
        return [{"id": int(i)} for i in ids]

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
