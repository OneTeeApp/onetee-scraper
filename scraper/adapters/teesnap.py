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

    @staticmethod
    def _window_courses_json(html: str) -> list[dict]:
        """Return the TOP-LEVEL objects of `window.courses = [...]`.

        Bracket-matched and JSON-parsed rather than regexed, because a course
        object embeds its parent property object — which has its own
        `"id":<n>,"created_at"` pair. Anchoring on that pattern therefore
        harvested property ids and passed them off as course ids; see
        discover_courses. Returns [] if the array can't be parsed, so the
        regex fallback still applies.
        """
        m = re.search(r"window\.courses\s*=\s*", html)
        if not m:
            return []
        try:
            start = html.index("[", m.end())
        except ValueError:
            return []
        depth, i, in_str, esc = 0, start, False, False
        while i < len(html):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(html[start:i + 1])
                    except Exception:  # noqa: BLE001 — fall back to regex
                        return []
                    return [c for c in data if isinstance(c, dict)]
            i += 1
        return []

    def discover_courses(self, sub: str) -> list[dict]:
        """Course ids (and names) from the homepage-inlined `window.courses`.

        Only the TOP-LEVEL entries of that array are courses. Each one embeds
        its parent property (`"property": {"id": 1329, "created_at": ...}`),
        and the old regex — anchored on `"id":<n>,"created_at"` anywhere in the
        region — swept those property ids up as if they were courses. On most
        tenants that was merely wasteful (a bogus id answers 200 with an empty
        list), but Hollydot's property id 1329 and Petteys Park's 1081 answer
        HTTP 500 "Be right back.", which killed the whole fetch and lost the
        60-70 real slots the course's own id was returning. See
        probe-results/diag4.txt section B.

        Disabled/deleted courses are skipped; a live one that simply has no
        sheet for the day answers 200 with an empty list, which is harmless.
        """
        html = self._get_text(f"https://{sub}.teesnap.net/")
        out: list[dict] = []
        for c in self._window_courses_json(html):
            cid = c.get("id")
            if not isinstance(cid, int) or c.get("deleted_at"):
                continue
            if c.get("enabled") is False:
                continue
            if c.get("key") is None and c.get("name") is None:
                continue
            out.append({"id": cid, "name": (c.get("name") or "").strip()})
        if out:
            return out
        # Fallback for a shape we can't parse: the old anchor, which
        # over-collects but is better than returning nothing.
        start = html.find("window.courses")
        region = html[start:start + 30000] if start >= 0 else html
        ids: list[str] = []
        for i in re.findall(r'"id":\s*(\d+)\s*,\s*"created_at"', region):
            if i not in ids:
                ids.append(i)
        if not ids:
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
            names = {c["id"]: (c.get("name") or course["name"])
                     for c in discovered}
        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no Teesnap course id in "
                               "window.courses")

        multi = len(course_ids) > 1
        out: list[TeeTime] = []
        errors: list[str] = []
        for cid in course_ids:
            # One bad id must not cost us the whole venue. Teesnap answers 500
            # ("Be right back.") for ids that aren't really courses, and for
            # genuinely broken sheets; the other ids on the same tenant keep
            # working. Collect and only raise if EVERY id failed.
            try:
                data = self.get_json(
                    f"https://{sub}.teesnap.net/customer-api/teetimes-day",
                    params={"course": cid, "date": date.isoformat(),
                            "players": 1, "holes": 18, "addons": "off"})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"course {cid}: {type(exc).__name__}: {exc}")
                continue
            block = (data or {}).get("teeTimes", {})
            for slot in block.get("teeTimes", []):
                prices = [float(p["price"]) for p in slot.get("prices", [])
                          if p.get("price") not in (None, "")]
                holes = sorted({18 if p.get("roundType") == "EIGHTEEN_HOLE"
                                else 9 for p in slot.get("prices", [])})
                # Teesnap has two slot shapes: older sheets nest the time in
                # teeOffSections[].turnTo.time; newer ones (e.g. Pagosa Springs)
                # put the ISO time at the slot's top-level "teeTime" and use
                # teeOffSections only for FRONT_NINE/BACK_NINE labels. Prefer the
                # nested times if present (preserves existing courses), else fall
                # back to the top-level teeTime.
                times = []
                for sec in slot.get("teeOffSections", []) or []:
                    t = (sec.get("turnTo") or {}).get("time") or sec.get("time")
                    if t:
                        times.append(t)
                if not times and slot.get("teeTime"):
                    times.append(slot["teeTime"])
                for t in times:
                    out.append(self.base_tee_time(
                        course,
                        teetime=str(t),
                        course_label=(names.get(cid) or "") if multi else "",
                        holes=holes,
                        open_spots=None,  # derive from bookings if needed later
                        price_min=min(prices) if prices else None,
                        price_max=max(prices) if prices else None,
                        raw={"course_name": names.get(cid, course["name"])},
                    ))
        if errors and len(errors) == len(course_ids):
            raise RuntimeError(f"{course['slug']}: every Teesnap course id "
                               f"failed — " + "; ".join(errors))
        return out
