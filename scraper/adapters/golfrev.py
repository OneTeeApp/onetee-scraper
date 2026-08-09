"""GolfRev adapter — golfrev.com, Cybergolf's tee-time reservation engine.

Anonymous, stateless HTML — but golfrev.com sits behind Cloudflare, which
fingerprints the TLS handshake (JA3). Plain python-requests from a datacenter
runner gets HTTP 403 (a ~6KB Cloudflare block page) no matter what headers it
sends; a real browser's handshake gets 200. This is NOT an IP-reputation block
(no proxy needed) and NOT header-based — proved by probe_golfrev.py from a
GitHub Actions runner:

    plain requests + stock UA     -> 403
    plain requests + full Chrome headers -> 403
    curl_cffi impersonate=chrome  -> 200, real cards       <-- the fix
    curl_cffi + a datacenter proxy-> unnecessary

So this adapter fetches with **curl_cffi** (Chrome TLS impersonation) instead of
the base requests session. No proxy, no headless browser — it runs on the plain
tiers like any other adapter, it just needs curl_cffi installed (the scrape
workflows pip-install it).

One GET per course-day returns an HTML fragment of Bootstrap cards:

    GET golfrev.com/go/tee_times/teetime_table_html.asp
        ?c=<courseid>&s=<M/D/YYYY>&h=<htc>&specials=&reset=yes&snapshot=no

A date past the booking window returns a tiny fragment (200, zero cards), so
out-of-window is naturally empty rather than an error. Each card:

    onClick="showBooking('2026-08-11',9431,14,16,4,0,'2174186',0,0,0,'');"
       -> date, sheetId, HOUR(24h), MINUTE, PLAYERS, ..., bookingId, ...
    <h5 class="card-title ...">2:16 PM</h5>                 human time
    <p class="card-text text-secondary ...">Birch Creek ...</p>  course
    <p class="card-text ...">4 players</p>                  open_spots
    <p class="... cust-card-trim">$23.00 - $46.00</p>       price min - max

hour/minute come from the onClick args (24h, unambiguous); the player count and
price banner from the same card. holes are not stated per slot (the price
*range* spans the 9- and 18-hole rates), so holes is left empty.

Registry ids: {"courseid": "<c>", "htc": "<h>"} — both from the golfrev URL.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter, RETRY_STATUS, TIMEOUT
from ..models import TeeTime

try:
    # Chrome TLS impersonation — clears golfrev's Cloudflare JA3 fingerprint
    # check that plain requests fails. The scrape workflows pip-install it.
    from curl_cffi import requests as creq
except Exception:  # noqa: BLE001 - missing dep is surfaced per-fetch, not at import
    creq = None

IMPERSONATE = "chrome"
URL = "https://www.golfrev.com/go/tee_times/teetime_table_html.asp"

# Head of a card's onClick, right after the "showBooking(" the html is split on:
# 'YYYY-MM-DD', sheetId, hour24, minute, players, ...
_ARGS = re.compile(
    r"^\s*'(\d{4}-\d{2}-\d{2})'\s*,\s*\d+\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d+)")
# Dollar amounts inside a single card (e.g. "$23.00 - $46.00").
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")


class GolfRevAdapter(Adapter):
    platform = "golfrev"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        if creq is None:
            raise RuntimeError(
                "golfrev: curl_cffi is not installed; it is required to clear "
                "golfrev.com's Cloudflare TLS fingerprinting")
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

    # -- HTTP (curl_cffi, Chrome-impersonated) -------------------------------

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
                r = creq.get(URL, params=params, impersonate=IMPERSONATE,
                             timeout=TIMEOUT)
                if r.status_code in RETRY_STATUS:
                    continue
                r.raise_for_status()
                return r.text
            except Exception:  # noqa: BLE001 - curl_cffi errors aren't requests errors
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
