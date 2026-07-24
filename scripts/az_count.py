"""D1 coverage by state + AZ platform breakdown of what's not yet active."""
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
        "SELECT DISTINCT course_slug FROM tee_times "
        "WHERE active=1 AND substr(teetime,1,10) BETWEEN ? AND ?", [D0, D2])
    active = {r["course_slug"] for r in rows}
    reg = load_registry("registry.json")
    byslug = {c["slug"]: c for c in reg}
    co = [c for c in reg if c.get("state") == "CO"]
    az = [c for c in reg if c.get("state") == "AZ"]
    co_active = [c for c in co if c["slug"] in active]
    az_active = [c for c in az if c["slug"] in active]
    print(f"D1 active {D0}..{D2}: total {len(active)}", flush=True)
    print(f"  CO active: {len(co_active)}/{len(co)}", flush=True)
    print(f"  AZ active: {len(az_active)}/{len(az)}", flush=True)
    print("AZ ACTIVE by platform:", flush=True)
    for p, n in Counter(c["platform"] for c in az_active).most_common():
        print(f"    {p:<22} {n}", flush=True)
    print("AZ NOT-active by platform:", flush=True)
    az_missing = [c for c in az if c["slug"] not in active]
    for p, n in Counter(c["platform"] for c in az_missing).most_common():
        print(f"    {p:<22} {n}", flush=True)

if __name__ == "__main__":
    main()
