"""TenFore (fox.tenfore.golf / swan.tenfore.golf) adapter.

TenFore is a SHARED host: one instance (`fox.tenfore.golf/<vanity>`) fronts many
courses (all of Montgomery County MD's municipals, plus a few others), and the
JSON API lives at `swan.tenfore.golf`, keyed by an integer `golfCourseId`.

Three endpoints (all GET, host swan.tenfore.golf), discovered 2026-08-06
(see claude/tenfore-cracked-2026-08-06.md):

  * /api/GolfCourse/GetGolfCourseByVanity?vanityName=<slug>   (OPEN)
        -> {golfCourseID, name, subCourseAlias, city, state, ...}
  * /api/TeeSheet?golfCourseId=&startDate=&endDate=           (OPEN, but useless
        alone: includes maintenance "Block" rows and carries NO online price)
  * /api/TeeTimes/Search?golfCourseIds=&dateFrom=&dateTo=&players=&holes=
        (reCAPTCHA-GATED) -> flat array of PRICED, bookable online slots.

Only the third gives the customer-facing priced availability, and it requires a
reCAPTCHA Enterprise token in three headers:
    x-recaptcha-action: teetimes_search
    x-recaptcha-token:  <token from grecaptcha.enterprise.execute(SITE_KEY, {action})>
    x-tenfore-appid:    23
Minting the token needs a real browser with grecaptcha loaded, so TenFore is a
BROWSER-TIER platform — see scraper/browser_tenfore.py. This module holds the
constants + the pure row->TeeTime parser so the browser module stays thin.

Registry `ids`: {"vanity": "<slug>", "golf_course_id": "<int>"}.

Search params that matter (learned the hard way):
  * dateFrom/dateTo are DATETIMES, not dates: "<YYYY-MM-DD>T00:00:00" ..
    "<YYYY-MM-DD>T23:59:59". A bare date returns [] (empty window).
  * A single call is ONE day (dateFrom/dateTo must be the same day).
  * players=1 is the widest net (TenFore does not refuse single-player here).
  * holes matters: 18-hole and 9-hole are SEPARATE result sets. Sligo Creek is
    9-hole only (holes=18 -> 0, holes=9 -> 58), so we query BOTH and merge.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://swan.tenfore.golf/api"
SITE_KEY = "6LfAN9ksAAAAAFxnXFLRCuU9gUXs6U6egm6TrjIn"
APP_ID = "23"
RECAPTCHA_ACTION = "teetimes_search"
# Both horizons a course might sell; each is a separate Search result set.
HOLES = (18, 9)


class TenForeAdapter(Adapter):
    platform = "tenfore"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        # Search is reCAPTCHA-gated; there is no plain-HTTP path. The live
        # fetch runs in scraper/browser_tenfore.py, which calls rows_to_teetimes.
        raise NotImplementedError(
            "TenFore requires a browser to mint a reCAPTCHA token; "
            "use scraper.browser_tenfore")

    @staticmethod
    def rows_to_teetimes(course: dict, date: dt.date,
                         rows_by_holes: dict[int, list]) -> list[TeeTime]:
        """Merge the per-holes Search result lists for one course+date into
        deduped TeeTime rows.

        `rows_by_holes` maps a holes value (18 / 9) to the raw Search array for
        that query. A slot (teeTimeId) can appear once per holes query and once
        per rate (teeFee), so we fold on teeTimeId: union the holes, min/max the
        price across rates, and take the largest open-spot count seen.
        """
        want = date.isoformat()
        agg: dict[Any, dict] = {}
        for holes, rows in rows_by_holes.items():
            for r in rows or []:
                ds = r.get("dateScheduled")
                if not ds:
                    continue
                # "2026-08-08T06:22:00-04:00" -> naive local "2026-08-08T06:22:00"
                iso = str(ds)[:19]
                if iso[:10] != want:      # guard: keep only the requested day
                    continue
                key = r.get("teeTimeId") or iso
                price = r.get("priceBeforeTax")
                mx, bk = r.get("maxPlayers"), r.get("bookedPlayers")
                open_spots = None
                if isinstance(mx, int) and isinstance(bk, int):
                    open_spots = max(mx - bk, 0)
                # sub-course label only when it names something other than the course
                sub = r.get("subCourseName") or r.get("subCourseAlias") or ""
                label = sub if sub and sub != r.get("golfCourseName") else ""
                a = agg.get(key)
                if a is None:
                    a = agg[key] = {"iso": iso, "holes": set(), "label": label,
                                    "pmin": None, "pmax": None, "open": None}
                a["holes"].add(int(holes))
                if isinstance(price, (int, float)):
                    a["pmin"] = price if a["pmin"] is None else min(a["pmin"], price)
                    a["pmax"] = price if a["pmax"] is None else max(a["pmax"], price)
                if open_spots is not None:
                    a["open"] = open_spots if a["open"] is None else max(a["open"], open_spots)

        out = []
        for a in agg.values():
            out.append(TenForeAdapter.base_tee_time(
                course,
                teetime=a["iso"],
                holes=sorted(a["holes"]),
                course_label=a["label"],
                open_spots=a["open"],
                price_min=a["pmin"],
                price_max=a["pmax"],
                raw={},
            ))
        out.sort(key=lambda t: t.teetime)
        return out
