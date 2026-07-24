"""Verify the real Club Caddie browser fetcher end-to-end for a few courses."""
from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")
from scraper import browser_clubcaddie              # noqa: E402
from scraper.aggregate import load_registry         # noqa: E402

TOMORROW = dt.date.today() + dt.timedelta(days=1)


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    want = ("applewood-golf-course", "salida-golf-club", "eaglevail-golf-club",
            "the-links-golf-course", "monte-vista-country-club")
    courses = [c for c in reg if c["slug"] in want and c["platform"] == "clubcaddie"]
    with sync_playwright() as pw:
        for c in courses:
            tts, err = browser_clubcaddie._fetch_course(pw, c, TOMORROW)
            eg = tts[0].to_dict() if tts else None
            print(f"RESULT cc {c['slug']:<34} {len(tts)} times err={err} "
                  f"e.g. {eg and (eg['teetime'], eg['holes'], eg['open_spots'], eg['price_min'])}",
                  flush=True)


if __name__ == "__main__":
    main()
