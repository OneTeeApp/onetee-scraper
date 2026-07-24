"""Coverage audit — the website shows ~80 courses but ~131 are wired. Find the
gap by comparing the registry against what's ACTUALLY in D1, then re-fetching
each missing plain-platform course to capture its real production error.

Output sections:
  1. D1 state: distinct courses with active tee times in the next 3 days,
     by platform (this is what the website can show).
  2. Registry vs D1 diff: wired courses with NO active rows.
  3. Live re-fetch of each missing plain-platform course (the browser-platform
     ones get their own note since they run on separate pipelines).
"""
from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")

from scraper.aggregate import load_registry           # noqa: E402
from scraper.d1 import D1Rest                         # noqa: E402
from scraper.aggregate import ADAPTERS                # noqa: E402

TODAY = dt.date.today()
DATES = [(TODAY + dt.timedelta(days=n)).isoformat() for n in range(3)]


def main():
    db = D1Rest()
    reg = load_registry("registry.json")
    by_slug = {c["slug"]: c for c in reg}

    rows = db.execute(
        "SELECT course_slug, platform, "
        "       SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS act, "
        "       COUNT(*) AS total, MAX(last_seen_at) AS seen "
        "FROM tee_times "
        "WHERE substr(teetime,1,10) >= ? AND substr(teetime,1,10) <= ? "
        "GROUP BY course_slug, platform",
        [DATES[0], DATES[-1]])
    in_d1 = {r["course_slug"]: r for r in rows}
    active = {s for s, r in in_d1.items() if (r["act"] or 0) > 0}

    print(f"== D1 {DATES[0]}..{DATES[-1]}: {len(in_d1)} courses present, "
          f"{len(active)} with ACTIVE times ==", flush=True)
    from collections import Counter
    plat_active = Counter(in_d1[s]["platform"] for s in active)
    for p, n in plat_active.most_common():
        print(f"  active {p:<14} {n}", flush=True)

    wired = [c for c in reg]
    missing = [c for c in wired if c["slug"] not in active]
    print(f"\n== {len(missing)} registry courses WITHOUT active D1 rows ==",
          flush=True)
    for c in sorted(missing, key=lambda x: (x["platform"], x["slug"])):
        d1r = in_d1.get(c["slug"])
        extra = (f"in-D1 total={d1r['total']} act={d1r['act']} seen={d1r['seen']}"
                 if d1r else "never landed")
        print(f"  {c['platform']:<14} {c['slug']:<42} {extra}", flush=True)

    # live re-fetch for missing plain-platform courses
    browser_platforms = {"clubprophet", "ezlinks", "golfnow"}
    print("\n== live re-fetch of missing PLAIN-platform courses (tomorrow) ==",
          flush=True)
    date = TODAY + dt.timedelta(days=1)
    for c in sorted(missing, key=lambda x: x["slug"]):
        plat = c["platform"]
        if plat in browser_platforms:
            continue
        cls = ADAPTERS.get(plat)
        if cls is None:
            print(f"  SKIP {c['slug']:<40} platform {plat} (no adapter)", flush=True)
            continue
        try:
            tts = cls().fetch(c, date)
            print(f"  OK   {c['slug']:<40} {len(tts)} times", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {c['slug']:<40} {type(e).__name__}: {str(e)[:110]}",
                  flush=True)

    print("\n(browser-platform gaps run on the cps/ezlinks/golfnow hourly "
          "pipelines — check those rows above)", flush=True)


if __name__ == "__main__":
    main()
# re-run audit v2
