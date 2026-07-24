"""Final authoritative D1 coverage count by platform."""
from __future__ import annotations
import datetime as dt, sys
sys.path.insert(0, ".")
from scraper.d1 import D1Rest
from scraper.aggregate import load_registry
from collections import Counter

TODAY = dt.date.today()
D0, D2 = TODAY.isoformat(), (TODAY + dt.timedelta(days=2)).isoformat()

def main():
    db = D1Rest()
    rows = db.execute(
        "SELECT DISTINCT course_slug, platform FROM tee_times "
        "WHERE active=1 AND substr(teetime,1,10) BETWEEN ? AND ?", [D0, D2])
    active = {r["course_slug"] for r in rows}
    plat = Counter(r["platform"] for r in rows)
    print(f"ACTIVE COURSES IN D1 ({D0}..{D2}): {len(active)}", flush=True)
    for p, n in plat.most_common():
        print(f"  {p:<14} {n}", flush=True)
    reg = load_registry("registry.json")
    missing = sorted({c["slug"] for c in reg} - active)
    print(f"\nREGISTRY COURSES NOT ACTIVE: {len(missing)}", flush=True)
    byp = Counter(c["platform"] for c in reg if c["slug"] in missing)
    for p, n in byp.most_common():
        print(f"  {p:<16} {n}", flush=True)
    for s in missing:
        c = next(x for x in reg if x["slug"] == s)
        print(f"    {c['platform']:<16} {s}", flush=True)

if __name__ == "__main__":
    main()
