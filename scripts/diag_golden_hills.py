"""Golden Hills (fka Arizona Golf Resort) 404s on kenna. Which part is wrong?

probe-results/verify_fixes.txt section A: alias=golden-hills-golf-club-az
answers HTTP 404 both with and without ?facilityIds=1295, while every other
pinned Arizona alias answers normally. A 404 from kenna means the alias is
not one it knows, so the registry's alias is stale or was never right — the
facility_id is a separate question and only matters once an alias resolves.

This asks three things, in order of what would fix the row:

  1. Does the booking page itself still exist, and what alias does it use?
     The x-be-alias value is what the page's own JS sends; the alias in a
     book.teeitup.com hostname is only a convention. Fetching the page and
     reading the alias out of it is ground truth.
  2. Do any plausible alias spellings resolve? A club that renamed usually
     keeps the old alias working for a while, so both names are worth a try.
  3. For whichever alias resolves, what facilities does it list, and does
     1295 appear among them?

A control alias that is known good runs first, so a blanket kenna outage is
distinguishable from a Golden-Hills-specific 404.

Public endpoints, report only. Nothing here edits the CSV, the registry, or
D1. No credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teeitup import API_BASE, TeeItUpAdapter  # noqa: E402

DATE = dt.date.today() + dt.timedelta(days=1)

CONTROL = "aguila-golf-course"       # known good, from section A
CANDIDATES = [
    "golden-hills-golf-club-az",     # what the registry has
    "golden-hills-golf-club",
    "goldenhills",
    "golden-hills",
    "arizona-golf-resort",           # the former name
    "arizona-golf-resort-az",
    "arizonagolfresort",
]

PAGES = [
    "https://golden-hills-golf-club-az.book.teeitup.com/?course=1295",
    "https://golden-hills-golf-club-az.book.teeitup.com/",
]


def status(ad: TeeItUpAdapter, url: str, headers=None, params=None) -> str:
    try:
        r = ad.session.get(url, headers=headers or {}, params=params or {},
                           timeout=25)
    except Exception as exc:  # noqa: BLE001
        return f"request failed: {type(exc).__name__}: {str(exc)[:70]}"
    body = ""
    try:
        body = json.dumps(r.json())[:180]
    except Exception:  # noqa: BLE001
        body = r.text[:180].replace("\n", " ")
    return f"HTTP {r.status_code}  {body}"


def main() -> None:
    print("diag_golden_hills: is the alias wrong, the facility id, or the club?")
    print(f"date probed: {DATE.isoformat()}")
    print("Report only. Nothing here edits the CSV, the registry, or D1.")
    ad = TeeItUpAdapter()

    print("\n" + "=" * 72)
    print("1. the booking page — does it exist, and what alias does it carry?")
    print("=" * 72)
    for url in PAGES:
        print(f"\n--- {url}")
        try:
            r = ad.session.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"    request failed: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        print(f"    HTTP {r.status_code}  {len(r.text)}B  final={r.url}")
        text = r.text
        found: list[str] = []
        for pat in (r'"alias"\s*:\s*"([a-z0-9\-]+)"',
                    r'x-be-alias["\']?\s*[:=]\s*["\']([a-z0-9\-]+)',
                    r'beAlias["\']?\s*[:=]\s*["\']([a-z0-9\-]+)',
                    r'ALIAS["\']?\s*[:=]\s*["\']([a-z0-9\-]+)'):
            for m in re.findall(pat, text):
                if m not in found:
                    found.append(m)
        print(f"    alias-looking strings in the page: {found[:10] or 'none'}")
        ids = sorted({int(i) for i in re.findall(r'facilityIds?["\']?\s*[:=]\s*'
                                                 r'["\']?(\d{2,6})', text)})
        print(f"    facility ids in the page: {ids[:10] or 'none'}")

    print("\n" + "=" * 72)
    print("2. which alias spellings does kenna know?")
    print("=" * 72)
    print(f"\n--- CONTROL {CONTROL}")
    print(f"    /v2/courses      {status(ad, f'{API_BASE}/v2/courses', {'x-be-alias': CONTROL})}")
    for alias in CANDIDATES:
        print(f"\n--- {alias}")
        h = {"x-be-alias": alias}
        print(f"    /v2/courses      {status(ad, f'{API_BASE}/v2/courses', h)}")
        print(f"    /alias/…/facilities "
              f"{status(ad, f'{API_BASE}/alias/{alias}/facilities', h)}")
        print(f"    /v2/tee-times    "
              f"{status(ad, f'{API_BASE}/v2/tee-times', h, {'date': DATE.isoformat()})}")
        sys.stdout.flush()

    print("\n" + "=" * 72)
    print("3. for anything that resolved, what does discover_facilities say?")
    print("=" * 72)
    for alias in CANDIDATES:
        try:
            fac = ad.discover_facilities(alias)
        except Exception as exc:  # noqa: BLE001
            print(f"    {alias:32s} raised {type(exc).__name__}: {str(exc)[:60]}")
            continue
        ids = [f.get("id") for f in fac if isinstance(f, dict)]
        names = [f.get("name") for f in fac if isinstance(f, dict)]
        print(f"    {alias:32s} {len(fac)} facilities ids={ids} names={names}")
        if 1295 in [i for i in ids if isinstance(i, int)] or "1295" in [str(i) for i in ids]:
            print("        -> the registry's facility_id 1295 IS in this alias")
        sys.stdout.flush()

    print("\ndone")


if __name__ == "__main__":
    main()
