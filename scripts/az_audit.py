"""Full Arizona coverage audit: registry vs live D1, with a live re-fetch of
every missing plain-platform course to categorize the reason (OK-but-empty,
error type, unsupported, browser-pending)."""
from __future__ import annotations
import datetime as dt, sys
from collections import Counter, defaultdict
sys.path.insert(0, ".")
from scraper.d1 import D1Rest
from scraper.aggregate import load_registry, ADAPTERS

TODAY = dt.date.today()
D0, D2 = TODAY.isoformat(), (TODAY + dt.timedelta(days=2)).isoformat()
REFETCH = dt.date.today() + dt.timedelta(days=1)
BROWSER = {"clubprophet", "ezlinks", "golfnow", "clubcaddie"}

def main():
    db = D1Rest()
    rows = db.execute(
        "SELECT DISTINCT course_slug FROM tee_times WHERE state='AZ' AND active=1 "
        "AND substr(teetime,1,10) BETWEEN ? AND ?", [D0, D2])
    active = {r["course_slug"] for r in rows}
    reg = load_registry("registry.json")
    az = [c for c in reg if c.get("state") == "AZ"]
    by_slug = {c["slug"]: c for c in az}
    az_active = [c for c in az if c["slug"] in active]
    missing = [c for c in az if c["slug"] not in active]

    print(f"=== ARIZONA COVERAGE: {len(az_active)}/{len(az)} active in D1 "
          f"({D0}..{D2}) ===", flush=True)
    print("ACTIVE by platform:", flush=True)
    for p, n in Counter(c["platform"] for c in az_active).most_common():
        tot = sum(1 for c in az if c["platform"] == p)
        print(f"    {p:<22} {n}/{tot}", flush=True)

    print(f"\n=== {len(missing)} MISSING, by platform ===", flush=True)
    for p, n in Counter(c["platform"] for c in missing).most_common():
        print(f"    {p:<22} {n}", flush=True)

    # live re-fetch the missing plain-platform courses to categorize
    print("\n=== live re-fetch of missing PLAIN-platform courses (tomorrow) ===",
          flush=True)
    cat = Counter()
    for c in sorted(missing, key=lambda x: (x["platform"], x["slug"])):
        plat = c["platform"]
        if plat.startswith("other:"):
            cat[f"{plat}:UNSUPPORTED"] += 1
            continue
        if plat in BROWSER:
            cat[f"{plat}:BROWSER-PENDING"] += 1
            continue
        cls = ADAPTERS.get(plat)
        if cls is None:
            cat[f"{plat}:NO-ADAPTER"] += 1
            continue
        try:
            tts = cls().fetch(c, REFETCH)
            if tts:
                cat[f"{plat}:OK-NOW"] += 1
                print(f"  OK    {plat:<10} {c['slug']:<40} {len(tts)}", flush=True)
            else:
                cat[f"{plat}:EMPTY"] += 1
                print(f"  EMPTY {plat:<10} {c['slug']:<40}", flush=True)
        except Exception as e:
            cat[f"{plat}:FAIL"] += 1
            print(f"  FAIL  {plat:<10} {c['slug']:<40} {type(e).__name__}: {str(e)[:70]}", flush=True)

    print("\n=== MISSING categorization ===", flush=True)
    for k, n in sorted(cat.items()): print(f"    {k:<28} {n}", flush=True)
    # list browser-pending + unsupported course names for clarity
    print("\n=== browser-pending (should land on hourly browser jobs) ===", flush=True)
    for c in missing:
        if c["platform"] in BROWSER:
            print(f"    {c['platform']:<12} {c['slug']}", flush=True)
    print("=== unsupported (new platforms, no adapter yet) ===", flush=True)
    for c in missing:
        if c["platform"].startswith("other:"):
            print(f"    {c['platform']:<22} {c['slug']}", flush=True)

if __name__ == "__main__":
    main()
