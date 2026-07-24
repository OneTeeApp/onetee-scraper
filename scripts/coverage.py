"""Coverage snapshot: active-in-D1 vs registry total, per state, with the gap
categorized (supported-but-pending vs new-platform vs unresolved)."""
from __future__ import annotations
import datetime as dt, sys
from collections import Counter
sys.path.insert(0, ".")
from scraper.d1 import D1Rest
from scraper.aggregate import load_registry

TODAY = dt.date.today()
D0, D2 = TODAY.isoformat(), (TODAY + dt.timedelta(days=2)).isoformat()
BROWSER = {"clubprophet", "ezlinks", "golfnow", "clubcaddie"}

def main():
    db = D1Rest()
    rows = db.execute(
        "SELECT state, COUNT(DISTINCT course_slug) AS n FROM tee_times "
        "WHERE active=1 AND substr(teetime,1,10) BETWEEN ? AND ? GROUP BY state",
        [D0, D2])
    active_by_state = {r["state"]: r["n"] for r in rows}
    act_rows = db.execute(
        "SELECT DISTINCT course_slug, state FROM tee_times WHERE active=1 "
        "AND substr(teetime,1,10) BETWEEN ? AND ?", [D0, D2])
    active = {(r["course_slug"]) for r in act_rows}
    reg = load_registry("registry.json")

    for st in ("CO", "AZ"):
        cs = [c for c in reg if c.get("state") == st]
        act = [c for c in cs if c["slug"] in active]
        miss = [c for c in cs if c["slug"] not in active]
        print(f"\n{'='*56}", flush=True)
        print(f"{st}: {len(act)}/{len(cs)} bookable courses active in D1 "
              f"({100*len(act)//max(len(cs),1)}%)", flush=True)
        print(f"{'='*56}", flush=True)
        print("  ACTIVE by platform:", flush=True)
        for p, n in Counter(c["platform"] for c in act).most_common():
            tot = sum(1 for c in cs if c["platform"] == p)
            print(f"    {p:<22} {n}/{tot}", flush=True)
        # categorize the gap
        cat = Counter()
        for c in miss:
            p = c["platform"]
            if p.startswith("other:"): cat["new-platform (no adapter)"] += 1
            elif p in BROWSER: cat["browser-pending / no-inventory"] += 1
            elif p == "chronogolf": cat["chronogolf-unresolved"] += 1
            else: cat["supported-pending / no-inventory"] += 1
        print(f"  MISSING ({len(miss)}):", flush=True)
        for k, n in cat.most_common(): print(f"    {k:<34} {n}", flush=True)

    tot_reg = sum(1 for c in reg if c.get("state") in ("CO", "AZ"))
    tot_act = sum(1 for c in reg if c.get("state") in ("CO","AZ") and c["slug"] in active)
    print(f"\n{'='*56}\nTOTAL CO+AZ: {tot_act}/{tot_reg} active "
          f"({100*tot_act//tot_reg}%)\nD1 active-by-state: {active_by_state}", flush=True)

if __name__ == "__main__":
    main()
