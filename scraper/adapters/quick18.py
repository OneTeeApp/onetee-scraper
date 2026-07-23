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

    @staticmethod
    def _column_holes(soup) -> dict[int, int]:
        """Map each column index to 9 or 18 holes using the header row.

        Quick18 tee sheets vary by site: some use the full 7-column matrix
        (Tee Time | Course | Players | 18 Walking | 18 Riding | 9 Walking |
        9 Riding), others a compact 3-column layout (Tee Time | Players |
        Price). We read hole counts off the header instead of assuming a fixed
        layout, so both parse correctly.
        """
        for tr in soup.find_all("tr"):
            headers = tr.find_all(["th", "td"])
            texts = [h.get_text(" ", strip=True).lower() for h in headers]
            if not any("hole" in t for t in texts):
                continue
            col: dict[int, int] = {}
            for i, t in enumerate(texts):
                if "18" in t:
                    col[i] = 18
                elif re.search(r"\b9\b", t):
                    col[i] = 9
            if col:
                return col
        return {}

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        from bs4 import BeautifulSoup  # lazy: isolate dep to this adapter
        sub = course["ids"]["subdomain"]
        html = self._get_html(
            f"https://{sub}.quick18.com/teetimes/searchmatrix",
            {"teedate": date.strftime("%Y%m%d")})
        soup = BeautifulSoup(html, "html.parser")
        col_holes = self._column_holes(soup)

        out: list[TeeTime] = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:                       # need time + at least one more
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

            # Scan every cell after the time: pull the players count from
            # whichever cell mentions "player", and prices from any cell with a
            # "$". Hole count for each price comes from that column's header
            # (col_holes); a single unlabeled rate defaults to 18 holes.
            players_txt = ""
            prices: list[float] = []
            holes: set[int] = set()
            for idx, c in enumerate(cells):
                if idx == 0:
                    continue
                txt = c.get_text(" ", strip=True)
                if not players_txt and PLAYERS_RE.search(txt):
                    players_txt = txt
                for p in PRICE_RE.findall(txt):
                    prices.append(float(p.replace(",", "")))
                    if idx in col_holes:
                        holes.add(col_holes[idx])
            if not holes and prices:
                holes.add(18)                        # single unlabeled rate

            pm = PLAYERS_RE.search(players_txt)
            spots = int(pm.group(2) or pm.group(1)) if pm else None

            out.append(self.base_tee_time(
                course,
                teetime=t.isoformat(),
                holes=sorted(holes),
                open_spots=spots,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw={"players": players_txt},
            ))

        if not out and "searchmatrix" not in html.lower() and "tee" not in html.lower():
            raise RuntimeError(f"{course['slug']}: unexpected Quick18 response "
                               "(no tee-sheet table found)")
        return out
