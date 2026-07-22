"""Quick18 adapter.

Tee sheets at https://<sub>.quick18.com/teetimes/searchmatrix?teedate=YYYYMMDD
are server-rendered HTML (a rate matrix table). We parse rows with regex —
defensive, best-effort; keep the raw cell text in `raw` for debugging.
Used in CO by: Keystone Ranch, Keystone River, Thorncreek, Homestead,
Beaver Creek.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

URL = "https://{sub}.quick18.com/teetimes/searchmatrix"

ROW_RE = re.compile(
    r'class="[^"]*matrixTeeTime[^"]*"[^>]*>.*?'
    r'(?P<time>\d{1,2}:\d{2}\s*[AP]M).*?</tr>',
    re.S | re.I)
PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d{2})?)")
SPOTS_RE = re.compile(r"(\d)\s*(?:golfer|player|spot)", re.I)


class Quick18Adapter(Adapter):
    platform = "quick18"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        sub = course["ids"]["subdomain"]
        r = self.session.get(URL.format(sub=sub),
                             params={"teedate": date.strftime("%Y%m%d")},
                             timeout=20)
        r.raise_for_status()
        html = r.text
        out: list[TeeTime] = []
        for m in ROW_RE.finditer(html):
            row_html = m.group(0)
            prices = [float(p) for p in PRICE_RE.findall(row_html)]
            spots = SPOTS_RE.findall(row_html)
            t = dt.datetime.strptime(
                f"{date.isoformat()} {m.group('time').replace(' ', '')}",
                "%Y-%m-%d %I:%M%p")
            out.append(self.base_tee_time(
                course,
                teetime=t.isoformat(),
                open_spots=max(int(s) for s in spots) if spots else None,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw={"row": row_html[:500]},
            ))
        if not out and "matrix" not in html.lower():
            raise RuntimeError(
                "Quick18 returned a page without a tee-time matrix — the "
                "portal may require a session cookie or different params; "
                "capture the request from a browser once and update ROW_RE.")
        return out
