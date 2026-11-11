"""Teesnap adapter — no auth, no CAPTCHA for viewing tee times (reCAPTCHA
only gates the actual booking step).

Discovery + fetch (both plain HTTP, both JSON):

  1. GET https://<sub>.api.teesnap.net/api/bookingsite/property
       -> { success, data: { id, key, name, ...,
                             courses: [{ id, key, name, min_players,
                                         max_players, holes, enabled,
                                         deleted_at, property_id }, ...] } }
  2. GET https://<sub>.api.teesnap.net/api/bookingsite/teetimes-day
         ?course=<id>&date=YYYY-MM-DD&players=1&holes=18&addons=off
       -> { teeTimes: { teeTimes: [ { teeTime, prices:[{roundType, price}],
                                      teeOffSections:[{teeOff, bookings}] } ],
                        bookings: [...] } }

MOVED 2026 (found 2026-11-11). Teesnap rebuilt the booking site as a JS app:
the homepage at <sub>.teesnap.net no longer inlines `window.courses`, and
<sub>.teesnap.net/customer-api/teetimes-day now answers 200 with the app's
HTML shell instead of JSON. Every course failed with "no Teesnap course id in
<sub>'s window.courses" and the whole platform served nothing from about
2026-09-29. It is the same backend on a new host — the course list is the
same objects under `data.courses`, the tee sheet has the shape shown
above, and every quirk recorded here was re-measured on the new host that
day: heathergardens 148 -> 0 slots and 131 -> 78, hollydot's property id 1329
-> HTTP 500 "Be right back.", and hollydot's 1550 served from the
heathergardens host. Verified from a home connection and from two
GitHub-hosted runners with plain requests (run 42965653257): 200 JSON, no
challenge.

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
`data.courses`. There is deliberately no pattern-matching fallback, and pinned
ids are checked against discovery. Returning zero for a tenant we cannot read is
correct; guessing is not.
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from .base import Adapter, PartialFetchError
from ..models import TeeTime

# Booking-site API root for one tenant. The tenant label is decorative for
# teetimes-day (ids resolve globally — see the docstring) but it is what makes
# `property` return THIS tenant's course list, which is the only thing that
# makes an id safe to use.
API = "https://{sub}.api.teesnap.net/api/bookingsite"


class TeesnapAdapter(Adapter):
    platform = "teesnap"

    # subdomain -> discovered top-level courses. The fleet scrapes several
    # dates per course, and the property answer does not change between them;
    # Teesnap also resets connections from datacenter IPs, so every fetch we
    # can skip is one less chance to trip _get_property's retry loop.
    _COURSES: dict[str, list[dict]] = {}
    _COURSES_LOCK = threading.Lock()

    def _get_property(self, sub: str) -> Any:
        """GET the tenant's property document with retry — Teesnap
        intermittently resets the connection (ConnectionResetError 104) from
        datacenter IPs; a retry almost always succeeds on the next attempt."""
        import time
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.session.get(API.format(sub=sub) + "/property",
                                     timeout=20)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001 — connection resets included
                last = e
                if attempt < 3:
                    time.sleep(1.0 + attempt)
        raise last  # type: ignore[misc]

    def discover_courses(self, sub: str) -> list[dict]:
        """Course ids (and names) from the tenant's own `data.courses`.

        Only the TOP-LEVEL entries of that list are courses. The property
        document carries other ids — its own (`data.id`, e.g. Hollydot 1329,
        Petteys Park 1081, which answer HTTP 500 "Be right back." when asked
        for as a course and used to kill the whole fetch), each course's
        `property_id`, and notice rows — and because `?course=` resolves
        globally (see the module docstring) an id that is not a course here
        returns another club's sheet rather than failing. So nothing is
        recovered by pattern: a document without a `data.courses` list yields
        [], the caller raises, and that is the honest outcome.

        Disabled/deleted courses are skipped; a live one that simply has no
        sheet for the day answers 200 with an empty list, which is harmless.
        """
        doc = self._get_property(sub)
        data = doc.get("data") if isinstance(doc, dict) else None
        courses = data.get("courses") if isinstance(data, dict) else None
        if not isinstance(courses, list):
            return []
        out: list[dict] = []
        for c in courses:
            if not isinstance(c, dict):
                continue
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
            # data.courses. data.courses lists only the top-level sheets, but
            # some tenants (heathergardens) expose their real bookable sheet only
            # under a property/teetimes id (131) that never appears there — its
            # top-level id (148) is an empty placeholder. teetimes-day resolves
            # ids globally, so a deliberately-pinned, verified id is valid. The
            # global id space still means a WRONG pin serves another club's
            # sheet, so we warn on any pinned id not in data.courses — but we
            # use it, because the operator pinned it on purpose after verifying.
            owned = {c["id"] for c in discovered}
            course_ids = [int(i) for i in pinned]
            foreign = [i for i in course_ids if i not in owned]
            if foreign:
                print(f"    {course['slug']}: pinned Teesnap id(s) {foreign} not "
                      f"in {sub}'s property courses — using anyway (explicit pin; "
                      f"teetimes-day ids are global, see adapters/teesnap.py)")
        else:
            course_ids = [c["id"] for c in discovered]

        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no Teesnap course id in "
                               f"{sub}'s property courses")

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
                    API.format(sub=sub) + "/teetimes-day",
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
                # teeOffSections[].turnTo.time; newer ones (Pagosa Springs, and
                # every sheet seen on the 2026 API) put the ISO time at the
                # slot's top-level "teeTime" and use
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
