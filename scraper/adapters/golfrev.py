"""GolfRev adapter — golfrev.com, Cybergolf's tee-time reservation engine.

NOT WIRED INTO ADAPTERS (2026-08-08). golfrev.com sits behind Cloudflare, which
serves the tee sheet fine to a residential browser (any/no headers) but returns
HTTP 403 (a 6.1KB Cloudflare block page, cf-ray ...-ORD, server=cloudflare) to a
GitHub Actions datacenter IP. Confirmed end-to-end via diag-course-pipeline: all
three days 403, zero cards. So a plain requests fetch from CI cannot work — this
platform needs the headless + residential-proxy tier (a browser_golfrev.py, the
way trutee/teeitup/cps are handled). This module is kept as the endpoint spec
and the ready-made card parser (_parse) that a future browser adapter reuses;
Birch Creek is marked `experimental` and excluded from the plain tiers until
that browser adapter exists.

Anonymous, stateless HTML. A Cybergolf course (e.g. Birch Creek, Smithfield UT)
fronts its booking on its own site, but the actual tee sheet is served by
golfrev.com. One GET per course-day returns an HTML fragment of Bootstrap cards:

    GET golfrev.com/go/tee_times/teetime_table_html.asp
        ?c=<courseid>&s=<M/D/YYYY>&h=<htc>&specials=&reset=yes&snapshot=no

Captured live 2026-08-08 (Birch Creek, courseid 3719, htc 370). No cookies /
session are needed — verified with credentials omitted. A date past the booking
window returns an empty fragment (200, zero cards), so out-of-window is
naturally empty rather than an error.

Each card (tags shown; whitespace collapsed):

    onClick="showBooking('2026-08-09',9431,14,16,4,0,'2174186',0,0,0,'');"
       -> date, sheetId, HOUR(24h), MINUTE, PLAYERS, ..., bookingId, ...
    <h5 class="card-title ...">2:16 PM</h5>                 human time
    <p class="card-text text-secondary ...">Birch Creek ...</p>  course
    <p class="card-text ...">4 players</p>                  open_spots
    <p class="... cust-card-trim">$23.00 - $46.00</p>       price min - max

The hour/minute come from the onClick args (24h, unambiguous); the price banner
and player count are read from the same card. holes are not stated per slot
(the price *range* spans the 9- and 18-hole rates), so holes is left empty.

Registry ids: {"courseid": "<c>", "htc": "<h>"} — both live in the golfrev URL.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

import requests

from .base import Adapter, RETRY_STATUS, TIMEOUT
from ..models import TeeTime

URL = "https://www.golfrev.com/go/tee_times/teetime_table_html.asp"

# The head of a card's onClick, right after the "showBooking(" the html is split
# on: 'YYYY-MM-DD', sheetId, hour24, minute, players, ...
_ARGS = re.compile(
    r"^\s*'(\d{4}-\d{2}-\d{2})'\s*,\s*\d+\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d+)")
# Dollar amounts inside a single card (e.g. "$23.00 - $46.00").
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


class GolfRevAdapter(Adapter):
    platform = "golfrev"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        cid = ids.get("courseid")
        htc = ids.get("htc")
        if not cid:
            raise ValueError(
                "golfrev: registry must pin ids.courseid "
                "(from golfrev.com/go/tee_times/?courseid=<c>)")
        html = self._get_html(str(cid), str(htc) if htc else "", date)
        if not html:
            return []
        return self._parse(course, html, date)

    # -- HTTP ----------------------------------------------------------------

    def _get_html(self, cid: str, htc: str, date: dt.date) -> str | None:
        params = {
            "c": cid,
            # golfrev wants M/D/YYYY with no leading zeros (matches the widget).
            "s": f"{date.month}/{date.day}/{date.year}",
            "h": htc,
            "specials": "",
            "reset": "yes",
            "snapshot": "no",
        }
        for attempt in range(2):
            try:
                r = self.session.get(URL, params=params, timeout=TIMEOUT)
                if r.status_code in RETRY_STATUS:
                    continue
                r.raise_for_status()
                return r.text
            except requests.RequestException:
                if attempt == 0:
                    continue
                return None
        return None

    # -- parsing -------------------------------------------------------------

    def _parse(self, course: dict, html: str, date: dt.date) -> list[TeeTime]:
        out: list[TeeTime] = []
        # One chunk per card: split on the booking call, so each chunk holds
        # exactly that slot's args + its own price banner (up to the next card).
        for chunk in html.split("showBooking(")[1:]:
            m = _ARGS.search(chunk)
            if not m:
                continue
            d_s, hh, mm, players = m.groups()
            if d_s != date.isoformat():
                # Defensive: the sheet echoed a different day than requested.
                continue
            try:
                t = dt.time(int(hh), int(mm))
            except ValueError:
                continue
            teetime = dt.datetime.combine(date, t).isoformat(timespec="seconds")
            spots = int(players)
            prices = [float(p) for p in _PRICE.findall(chunk) if float(p) > 0]
            pmin = min(prices) if prices else None
            pmax = max(prices) if prices else None
            out.append(self.base_tee_time(
                course,
                teetime=teetime,
                holes=[],
                open_spots=spots or None,
                price_min=pmin, price_max=pmax,
                raw={"hour": hh, "minute": mm, "players": players,
                     "prices": prices},
            ))
        return out
