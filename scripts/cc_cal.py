"""Verify the real Club Caddie fetcher end-to-end across 3 dates."""
from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")
from scraper import browser_clubcaddie              # noqa: E402
from scraper.aggregate import load_registry         # noqa: E402

TODAY = dt.date.today()
DATES = [TODAY + dt.timedelta(days=n) for n in range(3)]


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    want = ("applewood-golf-course", "salida-golf-club", "eaglevail-golf-club",
            "the-links-golf-course", "monte-vista-country-club",
            "black-canyon-golf-course", "conquistador-golf-course")
    courses = [c for c in reg if c["slug"] in want and c["platform"] == "clubcaddie"]
    with sync_playwright() as pw:
        for c in courses:
            got, err = browser_clubcaddie._fetch_course(pw, c, DATES)
            counts = {d.isoformat()[5:]: len(got.get(d.isoformat(), [])) for d in DATES}
            eg = None
            for d in DATES:
                v = got.get(d.isoformat())
                if v:
                    eg = v[0].to_dict()
                    break
            print(f"RESULT cc {c['slug']:<32} err={err} counts={counts} "
                  f"e.g.={eg and (eg['teetime'], eg['holes'], eg['price_min'], eg['open_spots'])}",
                  flush=True)


if __name__ == "__main__":
    main()
