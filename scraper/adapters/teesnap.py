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

THE HAZARD THAT SHAPES THIS FILE — `?course=` IS RESOLVED GLOBALLY.

The tenant subdomain is decorative. teetimes-day looks the id up in one
system-wide table and does not check that the id belongs to the host that
asked. Measured directly (probe-results/diag_teesnap2.txt): ten ids were
requested against four different tenants on the same date and every single
one came back byte-identical on all four — `?course=966` returned the same
63 times, the same 41.00/60.00 prices and the same fingerprint d6cb518250e1
from heathergardens, lakehavasu, mtmassivegolf and sundancegolfclub alike,
and the control id 1 returned HTTP 500 from all four rather than "unknown
here".

The consequence is that a wrong id does not fail loudly — it succeeds with
somebody else's tee sheet, which we would then publish under our course's
name. That is a worse bug than capturing nothing. probe-results/
diag_teesnap.txt has the receipts: heathergardens' property_id 131 answers
with 78 slots while its one real course (148) is empty for the day, and text
notices out of `courses[].infos` — lakehavasu 1517, sundance 1785 — answer
with 60 slots each. None of those are courses on those tenants.

So: an id may be used ONLY if it is a top-level entry of that tenant's own
`window.courses`. There is deliberately no regex fallback, and pinned ids
are checked against discovery. Returning zero for a tenant we cannot read is
correct; guessing is not.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from typing import Any

from .base import Adapter, PartialFetchError
from ..models import TeeTime

# (There used to be a COURSES_RE non-greedy `window.courses = (\[.*?\]);`
# here. It is gone on purpose: it stops at the first "];" inside a nested
# array, and anything that recovers ids by pattern rather than by parsing can
# hand us an id this tenant does not own. Parse or fail — see the docstring.)


class TeesnapAdapter(Adapter):
    platform = "teesnap"

    # subdomain -> discovered top-level courses. The fleet scrapes several
    # dates per course, and the homepage answer does not change between them;
    # Teesnap also resets connections from datacenter IPs, so every fetch we
    # can skip is one less chance to trip _get_text's retry loop.
    _COURSES: dict[str, list[dict]] = {}
    _COURSES_LOCK = threading.Lock()

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

        There is NO regex fallback. The old one anchored on `"id":<n>,
        "created_at"` across a 30000-char slice, which matches the embedded
        property object and every row of `courses[].infos` — and because
        `?course=` resolves globally (see the module docstring), those ids
        return other clubs' sheets rather than failing. An unparseable
        homepage now yields [], and the caller raises, which is the honest
        outcome.
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
        return out

    def _courses_cached(self, sub: str) -> list[dict]:
        """discover_courses() memoised per subdomain. Raises on fetch failure."""
        with self._COURSES_LOCK:
            if sub in self._COURSES:
                return self._COURSES[sub]
        found = self.discover_courses(sub)
        with self._COURSES_LOCK:
            self._COURSES[sub] = found
        return found

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        sub = course["ids"]["subdomain"]
        pinned = course["ids"].get("teesnap_course_ids")
        discovered = self._courses_cached(sub)
        names = {c["id"]: (c.get("name") or course["name"])
                 for c in discovered}

        if pinned:
            # An explicit pin is TRUSTED, even if the id is not in this tenant's
            # window.courses. window.courses lists only the top-level sheets, but
            # some tenants (heathergardens) expose their real bookable sheet only
            # under a property/teetimes id (131) that never appears there — its
            # top-level id (148) is an empty placeholder. teetimes-day resolves
            # ids globally, so a deliberately-pinned, verified id is valid. The
            # global id space still means a WRONG pin serves another club's
            # sheet, so we warn on any pinned id not in window.courses — but we
            # use it, because the operator pinned it on purpose after verifying.
            owned = {c["id"] for c in discovered}
            course_ids = [int(i) for i in pinned]
            foreign = [i for i in course_ids if i not in owned]
            if foreign:
                print(f"    {course['slug']}: pinned Teesnap id(s) {foreign} not "
                      f"in {sub}'s window.courses — using anyway (explicit pin; "
                      f"teetimes-day ids are global, see adapters/teesnap.py)")
        else:
            course_ids = [c["id"] for c in discovered]

        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no Teesnap course id in "
                               f"{sub}'s window.courses")

        multi = len(course_ids) > 1
        out: list[TeeTime] = []
        errors: list[str] = []
        failed: list[tuple[int, str]] = []   # (cid, error text)
        for cid in course_ids:
            # One bad id must not cost us the whole venue. Teesnap answers 500
            # ("Be right back.") for ids that aren't really courses, and for
            # genuinely broken sheets; the other ids on the same tenant keep
            # working. Collect; only a total failure is a plain raise — a
            # PARTIAL failure raises PartialFetchError, which publishes the
            # served sheets while its label records shield the failed sheet's
            # existing D1 rows from deactivation (they used to be erased,
            # because the venue counted as scraped).
            try:
                data = self.get_json(
                    f"https://{sub}.teesnap.net/customer-api/teetimes-day",
                    params={"course": cid, "date": date.isoformat(),
                            "players": 1, "holes": 18, "addons": "off"})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"course {cid}: {type(exc).__name__}: {exc}")
                failed.append((cid, f"{type(exc).__name__}: {exc}"))
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
        if failed:
            # Partial: publish `out` (the served sheets) and shield the failed
            # sheets' rows via their labels. A label can be "" only when the
            # venue has one id, and one id failing is total failure above —
            # so every label here names a real sheet.
            raise PartialFetchError(
                f"{course['slug']}: {len(failed)} of {len(course_ids)} "
                "Teesnap course id(s) failed — " + "; ".join(errors),
                tee_times=out,
                failed_labels=[(names.get(cid) or "") if multi else ""
                               for cid, _ in failed])
        return out
