"""Verify the Chronogolf->X remaps: fetch each remapped plain-platform course
via its adapter, and try to resolve the 4 premium Troon courses' real TeeItUp
alias from their /book_tt/ page or candidate aliases."""
from __future__ import annotations
import datetime as dt, re, sys
import requests
sys.path.insert(0, ".")
from scraper.aggregate import load_registry, ADAPTERS
from scraper.adapters.base import USER_AGENT, make_session
from scraper.adapters.teeitup import API_BASE

DATE = dt.date.today() + dt.timedelta(days=1)
REMAPPED = {"troon-north-golf-club","scottsdale-silverado-golf-club","the-refuge-golf-club",
 "francisco-grande-hotel-golf-resort","elephant-rocks-golf-course","canyon-mesa-country-club",
 "orange-tree-golf-resort","dove-valley-ranch-golf-club","desert-lakes-golf-club",
 "snowflake-municipal-golf-course","cerbat-cliffs-golf-course","pinetop-lakes-golf-country-club"}

def main():
    reg = load_registry("registry.json")
    by = {c["slug"]: c for c in reg}
    print("=== verify remapped plain-platform courses (tomorrow) ===", flush=True)
    for slug in sorted(REMAPPED):
        c = by.get(slug)
        if not c: print(f"  {slug}: NOT IN REGISTRY"); continue
        cls = ADAPTERS.get(c["platform"])
        try:
            tts = cls().fetch(c, DATE)
            print(f"  {c['platform']:<9} {slug:<38} OK {len(tts)}", flush=True)
        except Exception as e:
            print(f"  {c['platform']:<9} {slug:<38} FAIL {type(e).__name__}: {str(e)[:70]}", flush=True)

    # resolve premium Troon TeeItUp aliases from their /book_tt/ pages
    print("\n=== resolve premium Troon aliases (/book_tt/) ===", flush=True)
    s = make_session()
    sites = {
        "the-phoenician": "https://www.golfthephoenician.com/book_tt/",
        "westin-kierland": "https://www.kierlandgolf.com/book_tt/",
        "boulders": "https://www.bouldersclub.com/",
        "lookout-mountain": "https://www.lookoutmountaingolf.com/book_tt/",
        "scottsdale-cc": "https://www.scottsdaleccgolf.com/book",
        "arizona-grand": "https://www.arizonagrandgolf.com/reserve-a-tee-time/",
    }
    for name, url in sites.items():
        try:
            r = s.get(url, timeout=20)
            html = r.text
            aliases = set(re.findall(r"([a-z0-9-]+)\.book(?:-v2)?\.teeitup\.(?:com|golf)", html))
            fu = set(re.findall(r"foreupsoftware\.com/index\.php/booking/(?:index/)?(\d+)", html))
            gn = set(re.findall(r"golfnow\.com/tee-times/facility/(\d+)", html))
            ez = set(re.findall(r"([a-z0-9-]+)\.ezlinksgolf\.com", html))
            print(f"  {name:<16} {r.status_code} teeitup={aliases or '-'} "
                  f"foreup={fu or '-'} golfnow={gn or '-'} ezlinks={ez or '-'}", flush=True)
        except Exception as e:
            print(f"  {name:<16} FAIL {type(e).__name__} {str(e)[:50]}", flush=True)

if __name__ == "__main__":
    main()
