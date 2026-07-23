"""Quick18 (SagaCity Golf) adapter.

Server-rendered tee sheet at:
    https://<sub>.quick18.com/teetimes/searchmatrix?teedate=YYYYMMDD

The response is an HTML table with columns:
    Tee Time | Course | Players | 18 Holes Walking | 18 Holes Riding
             | 9 Holes Walking | 9 Holes Riding
Each data row's first cell is a time like "6:50 AM"; the four rate columns hold
either "$NN.NN" (with a Select button) or "N/A / Rate not available".

We parse the table with BeautifulSoup (robust to class-name changes): any <tr>
whose first cell matches a time is a tee-time row. Verified against Thorncreek
(thorncreek.quick18.com) July 2026. Used in CO by Thorncreek, Homestead,
Beaver Creek, Keystone Ranch, Keystone River.
"""
from __future__ import annotations

import datetime as dt
import re
import time as _time
from typing import Any

from .base import Adapter
from ..models import TeeTime

TIME_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*([AaPp][Mm])")
PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
PLAYERS_RE = re.compile(r"(\d+)\s*(?:to\s*(\d+))?\s*player")


class Quick18Adapter(Adapter):
    platform = "quick18"

    def _get_html(self, url: str, params: dict) -> str:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=20)
                r.raise_for_status()
                return r.text
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < 3:
                    _time.sleep(1.0 + attempt)
        raise last  # type: ignore[misc]

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        from bs4 import BeautifulSoup  # lazy: isolate dep to this adapter
        sub = course["ids"]["subdomain"]
        html = self._get_html(
            f"https://{sub}.quick18.com/teetimes/searchmatrix",
            {"teedate": date.strftime("%Y%m%d")})
        soup = BeautifulSoup(html, "html.parser")

        out: list[TeeTime] = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            first = cells[0].get_text(" ", strip=True)
            m = TIME_RE.match(first)
            if not m:
                continue
            try:
                t = dt.datetime.strptime(
                    f"{date.isoformat()} {m.group(1)}{m.group(2).upper()}",
                    "%Y-%m-%d %I:%M%p")
            except ValueError:
                continue

            # cell layout: 0=time 1=course 2=players 3..6=rate columns
            course_name = cells[1].get_text(" ", strip=True) if len(cells) > 1 else course["name"]
            players_txt = cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""
            pm = PLAYERS_RE.search(players_txt)
            spots = int(pm.group(2) or pm.group(1)) if pm else None

            prices, holes = [], set()
            rate_cells = cells[3:7] if len(cells) >= 7 else cells[3:]
            for idx, c in enumerate(rate_cells):
                for p in PRICE_RE.findall(c.get_text(" ", strip=True)):
                    prices.append(float(p.replace(",", "")))
                    holes.add(18 if idx < 2 else 9)  # first two cols = 18h

            out.append(self.base_tee_time(
                course,
                teetime=t.isoformat(),
                holes=sorted(holes),
                open_spots=spots,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw={"course_name": course_name, "players": players_txt},
            ))

        if not out and "searchmatrix" not in html.lower() and "tee" not in html.lower():
            raise RuntimeError(f"{course['slug']}: unexpected Quick18 response "
                               "(no tee-sheet table found)")
        return out
