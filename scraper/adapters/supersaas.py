"""SuperSaaS public-schedule adapter (supersaas.com/schedule/<acct>/<sched>).

Small clubs (Spreading Antlers) publish their tee sheet as a public SuperSaaS
schedule. There's no JSON/iCal feed enabled, but the day view is server-rendered
HTML: open tee slots are clickable "new reservation" chips (class contains
`nr`), booked/blocked slots are `out`/`f`. We fetch the day view and scrape the
open chips.

  GET /schedule/<acct>/<sched>?year=Y&month=M&day=D&view=day
     -> HTML with <div class="chip nr">7:00am − 7:10am</div> per OPEN slot

Login is only required to *book*, not to view availability.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter
from ..models import TeeTime

BASE = "https://www.supersaas.com"
_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)", re.I)


class SuperSaasAdapter(Adapter):
    platform = "supersaas"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        acct, sched = ids.get("account"), ids.get("schedule")
        if not (acct and sched):
            raise ValueError(f"{course['slug']}: supersaas needs account + schedule")
        url = f"{BASE}/schedule/{acct}/{sched}"
        from bs4 import BeautifulSoup   # lazy: only the plain scrape installs bs4
        html = self.session.get(url, params={
            "year": date.year, "month": date.month, "day": date.day,
            "view": "day"}, timeout=25).text
        soup = BeautifulSoup(html, "html.parser")

        seen: set[str] = set()
        out: list[TeeTime] = []
        # open slots: a chip whose class marks it "nr" (new-reservation / free)
        for el in soup.select('[class~="nr"]'):
            cls = " ".join(el.get("class") or [])
            if "out" in cls or " f " in f" {cls} ":   # skip full/outside markers
                continue
            m = _TIME.search(el.get_text(" ", strip=True))
            if not m:
                continue
            hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
            if ap == "pm" and hh != 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            iso = dt.datetime.combine(date, dt.time(hh, mm)).isoformat()
            if iso in seen:
                continue
            seen.add(iso)
            out.append(self.base_tee_time(course, teetime=iso, holes=[],
                                          open_spots=None, price_min=None,
                                          price_max=None, raw={}))
        return out
