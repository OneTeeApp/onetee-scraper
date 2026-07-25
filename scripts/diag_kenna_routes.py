"""Two follow-ups from diag_golden_hills.txt.

1. THE ALIAS FIX. `golden-hills-golf-club` and `arizona-golf-resort` both
   resolve and both list exactly facility 1295 "Golden Hills Golf Club" —
   the registry's `golden-hills-golf-club-az` is simply not an alias kenna
   knows. Before editing the CSV, check whether the booking HOST follows the
   alias: if golden-hills-golf-club.book.teeitup.com serves the same shell,
   the -az booking_url we publish to users is broken too (its own JS would
   send the same 404-ing alias), and the URL should move. If the -az host is
   the only one that serves, the URL stays and the alias gets pinned in
   EXTRA_IDS instead.

2. /v2/courses MAY BE DEAD FLEET-WIDE. In that run the CONTROL alias
   (aguila-golf-course, which returns full sheets) also answered HTTP 404 on
   /v2/courses, and every alias that resolved did so on
   /alias/<alias>/facilities. discover_facilities tries /v2/courses FIRST, so
   if that is universal we are spending one wasted request per alias against
   a host that 429s the whole fleet — worth flipping the order, but only on
   more than one sample.

Public endpoints, report only. Nothing here edits the CSV, the registry, or
D1. No credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teeitup import API_BASE, TeeItUpAdapter  # noqa: E402
from scraper.aggregate import load_registry  # noqa: E402

DATE = dt.date.today() + dt.timedelta(days=1)

HOSTS = [
    "https://golden-hills-golf-club-az.book.teeitup.com/?course=1295",
    "https://golden-hills-golf-club.book.teeitup.com/?course=1295",
    "https://arizona-golf-resort.book.teeitup.com/?course=1295",
    # a host that certainly is a real tenant, for shape comparison
    "https://aguila-golf-course.book.teeitup.com/",
    # a host that certainly is not, to see whether the wildcard serves anything
    "https://this-club-does-not-exist-onetee.book.teeitup.com/",
]


def route(ad: TeeItUpAdapter, url: str, alias: str) -> str:
    try:
        r = ad.session.get(url, headers={"x-be-alias": alias}, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return f"failed {type(exc).__name__}: {str(exc)[:60]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return f"HTTP 200 non-JSON ({len(r.text)}B)"
    rows = data if isinstance(data, list) else data.get("courses", [])
    ids = [x.get("id") for x in rows if isinstance(x, dict)]
    return f"HTTP 200  {len(rows)} facilities ids={ids[:8]}"


def main() -> None:
    print("diag_kenna_routes: booking hosts, and which discovery route lives")
    print(f"date probed: {DATE.isoformat()}")
    print("Report only. Nothing here edits the CSV, the registry, or D1.")
    ad = TeeItUpAdapter()

    print("\n" + "=" * 72)
    print("1. does the booking HOST follow the alias?")
    print("=" * 72)
    for url in HOSTS:
        try:
            r = ad.session.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"    {url}\n        failed {type(exc).__name__}: {str(exc)[:70]}")
            continue
        print(f"    {url}\n        HTTP {r.status_code}  {len(r.text)}B  "
              f"final={r.url}")
        sys.stdout.flush()
    print("\n    Same byte size from a real tenant, a wrong alias and a "
          "nonsense one means the host is a wildcard SPA shell that resolves "
          "the alias client-side — in which case a 404-ing alias is a broken "
          "page for users, not just for us.")

    print("\n" + "=" * 72)
    print("2. /v2/courses vs /alias/<alias>/facilities, across live aliases")
    print("=" * 72)
    reg = load_registry("registry.json")
    aliases: list[str] = []
    for c in reg:
        a = c["ids"].get("alias") if c["platform"] == "teeitup" else None
        if a and a not in aliases:
            aliases.append(a)
    print(f"{len(aliases)} distinct teeitup aliases in the registry; "
          f"probing up to 12\n")
    v2_ok = alias_ok = 0
    for a in aliases[:12]:
        r1 = route(ad, f"{API_BASE}/v2/courses", a)
        r2 = route(ad, f"{API_BASE}/alias/{a}/facilities", a)
        v2_ok += r1.startswith("HTTP 200")
        alias_ok += r2.startswith("HTTP 200")
        print(f"    {a:34s} /v2/courses: {r1}")
        print(f"    {'':34s} /alias/../facilities: {r2}")
        sys.stdout.flush()
    print(f"\n    /v2/courses answered for {v2_ok}/{min(12, len(aliases))}; "
          f"/alias/<alias>/facilities answered for "
          f"{alias_ok}/{min(12, len(aliases))}.")
    print("    If /v2/courses is 0-for-N, discover_facilities should try the "
          "alias route FIRST and keep /v2/courses only as the fallback: one "
          "less request per alias against a host that 429s the fleet.")

    print("\n" + "=" * 72)
    print("3. the Golden Hills sheet, through the alias that resolves")
    print("=" * 72)
    for a in ("golden-hills-golf-club", "arizona-golf-resort"):
        try:
            data = ad._teetimes(a, DATE, "1295")
        except Exception as exc:  # noqa: BLE001
            print(f"    {a:26s} raised {type(exc).__name__}: {str(exc)[:70]}")
            continue
        blocks = data if isinstance(data, list) else [data]
        slots = [s for b in blocks for s in ((b or {}).get("teetimes", []) or [])]
        first = json.dumps(slots[0])[:160] if slots else ""
        print(f"    {a:26s} {len(slots)} slots on {DATE.isoformat()}")
        if first:
            print(f"        first: {first}")
        sys.stdout.flush()

    print("\ndone")


if __name__ == "__main__":
    main()
