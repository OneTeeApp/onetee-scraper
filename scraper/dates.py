"""Which calendar dates to scrape — 'today' is a per-timezone question.

The workflows used to compute dates with `date -u +%F`. That is wrong for any
course west of Greenwich: from 18:00 MDT onward the UTC date is already
tomorrow, so the "today" scan actually scraped tomorrow and the evening slots
of the real local today were never refreshed again — they just sat in D1 as
active=1 until something else deactivated them. (Combined with the read API not
filtering past times, that is how a 7:20am slot was still on the site at
4:17pm.)

This module answers the question properly: take every timezone the registry
actually covers, ask each one what today is, and scrape the union. Near a date
boundary that naturally yields one extra date (Denver has ticked over to the
25th while Phoenix is still on the 24th -> scrape both), which is exactly the
coverage we want.

    python -m scraper.dates --registry registry.json --days 3
    2026-07-24
    2026-07-25
    2026-07-26
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import zoneinfo

from .d1 import _STATE_TZ

DEFAULT_TZ = "America/Denver"


def covered_timezones(registry_path: str) -> list[str]:
    try:
        reg = json.loads(pathlib.Path(registry_path).read_text())["courses"]
    except Exception:  # noqa: BLE001 — no registry: fall back to home tz
        return [DEFAULT_TZ]
    tzs = {_STATE_TZ[c["state"]] for c in reg
           if c.get("state") in _STATE_TZ}
    return sorted(tzs) or [DEFAULT_TZ]


def scrape_dates(registry_path: str, days: int = 3) -> list[dt.date]:
    """Union of [local today .. local today + days-1] over covered timezones."""
    out: set[dt.date] = set()
    for tz in covered_timezones(registry_path):
        today = dt.datetime.now(zoneinfo.ZoneInfo(tz)).date()
        for d in range(days):
            out.add(today + dt.timedelta(days=d))
    return sorted(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--days", type=int, default=3)
    a = p.parse_args()
    for d in scrape_dates(a.registry, a.days):
        print(d.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
