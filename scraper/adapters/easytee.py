"""EasyTee adapter — app.easyteegolf.com, server-rendered HTML. Anonymous.

Captured live 2026-08-08 (Schneiter's Pebblebrook). One GET per day:

    GET app.easyteegolf.com/course/<slug>/?days=<N>

The server renders ONE day's sheet per N. `days` is a small offset, NOT an
absolute date, and its zero point tracks the server's own midnight rollover
(observed: days=3 rendered "Mon, Aug 10, 2026" when the runner date was
2026-08-08, i.e. today+2 -> days=3). So the offset the runner would compute can
be off by one across a timezone boundary.

THE ONE RULE THAT KEEPS THIS HONEST: the page states its own selected date in the
active/disabled date control. The adapter parses that rendered date and publishes
rows ONLY if it equals the requested date. It tries the computed N and its two
neighbours; if none render the requested date (e.g. a date past the booking
window, where the page clamps to the last available day), the sheet is empty
rather than one day's rows published under another day's name.

Registry ids: {"slug": "<path segment>"} (from app.easyteegolf.com/course/<slug>/).

Card shape (tag-stripped): "7:10 AM | 1 - 4 golfers | $20 | 9 Holes | Reserve".
`min - max golfers` is the bookable party range; max is the remaining capacity
(open_spots). A single "1 golfer" (no range) means one seat left.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

import requests

from .base import Adapter, RETRY_STATUS, TIMEOUT
from ..models import TeeTime

BASE = "https://app.easyteegolf.com/course/{slug}/"

# Active/disabled date control, e.g. ...class="dropdown-item disabled ...">Mon, Aug 10, 2026
_ACTIVE_DATE = re.compile(
    r'class="[^"]*\bdisabled\b[^"]*"[^>]*>\s*'
    r'([A-Z][a-z]{2},\s*[A-Z][a-z]{2}\s*\d{1,2},\s*\d{4})')

# One tee-time card in the tag-stripped stream.
_CARD = re.compile(
    r'(\d{1,2}:\d{2}\s*[AP]M)\s+'          # time
    r'(\d+)(?:\s*-\s*(\d+))?\s*golfers?\s+'  # min (- max) golfers
    r'\$(\d+(?:\.\d+)?)\s+'                # price
    r'(\d+)\s*Holes', re.IGNORECASE)      # holes


class EasyTeeAdapter(Adapter):
    platform = "easytee"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        slug = ids.get("slug")
        if not slug:
            raise ValueError(
                "easytee: registry must pin ids.slug "
                "(from app.easyteegolf.com/course/<slug>/)")

        base_n = (date - dt.date.today()).days + 1     # days=1 == today
        for n in (base_n, base_n - 1, base_n + 1):
            if n < 1:
                continue
            html = self._get_html(slug, n)
            if html is None:
                continue
            rendered = self._rendered_date(html)
            if rendered != date:                       # wrong day -> never publish
                continue
            return self._parse(course, html, date)
        return []

    # -- HTTP ----------------------------------------------------------------

    def _get_html(self, slug: str, days: int) -> str | None:
        url = BASE.format(slug=slug)
        for attempt in range(2):
            try:
                r = self.session.get(url, params={"days": days}, timeout=TIMEOUT)
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

    @staticmethod
    def _rendered_date(html: str) -> dt.date | None:
        m = _ACTIVE_DATE.search(html or "")
        if not m:
            return None
        try:
            return dt.datetime.strptime(
                re.sub(r"\s+", " ", m.group(1)).strip(), "%a, %b %d, %Y").date()
        except ValueError:
            return None

    def _parse(self, course: dict, html: str, date: dt.date) -> list[TeeTime]:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\s+", " ", text)
        out: list[TeeTime] = []
        for m in _CARD.finditer(text):
            time_s, lo, hi, price_s, holes_s = m.groups()
            teetime = self._iso(date, time_s)
            if teetime is None:
                continue
            spots = int(hi or lo)
            try:
                price = float(price_s)
            except ValueError:
                price = None
            price = price if (price and price > 0) else None
            h = int(holes_s)
            out.append(self.base_tee_time(
                course,
                teetime=teetime,
                holes=[h] if h in (9, 18) else [],
                open_spots=spots,
                price_min=price, price_max=price,
                raw={"time": time_s, "golfers": f"{lo}-{hi}" if hi else lo,
                     "price": price_s, "holes": holes_s},
            ))
        return out

    @staticmethod
    def _iso(date: dt.date, time_s: str) -> str | None:
        try:
            t = dt.datetime.strptime(re.sub(r"\s+", " ", time_s).strip().upper(),
                                     "%I:%M %p").time()
        except ValueError:
            return None
        return dt.datetime.combine(date, t).isoformat(timespec="seconds")
