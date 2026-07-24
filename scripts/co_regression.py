"""Full Colorado regression: run every CO booking source through its REAL
production fetcher for one date and report per-course capture, so we can see
exactly which courses capture tee times, which come back empty, and which error.

Run in CI (needs playwright+chromium for the browser fetchers):
    python scripts/co_regression.py [YYYY-MM-DD]
Writes a human-readable report to stdout (the workflow tees it to
probe-results/co_regression.txt).
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import sys

# Running `python scripts/co_regression.py` puts scripts/ on sys.path, not the
# repo root — add the repo root so `scraper` imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.aggregate import load_registry, run as agg_run  # noqa: E402

REG = "registry.json"

# platforms handled by the plain (non-browser) aggregator
PLAIN = {"foreup", "teeitup", "chronogolf", "membersports",
         "quick18", "teesnap", "foretees"}
# platforms handled by dedicated browser fetchers (module, single-date run())
BROWSER = {
    "clubprophet": "browser_cps",
    "ezlinks": "browser_ezlinks",
    "golfnow": "browser_golfnow",
    "supersaas": "browser_supersaas",
    # clubcaddie handled specially (its run() takes a list of dates)
}


def main() -> int:
    date = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
            else dt.date.today() + dt.timedelta(days=1))
    reg = load_registry(REG)
    co = [c for c in reg if c.get("state") == "CO"]
    json.dump({"courses": co}, open("co.json", "w"))
    print(f"=== Colorado regression for {date} — {len(co)} booking sources ===\n")

    counts: collections.Counter = collections.Counter()
    errs: dict[str, str] = {}
    ran_platforms: set[str] = set()

    def absorb(doc: dict) -> None:
        for t in doc.get("tee_times", []):
            counts[t["course_slug"]] += 1
        for e in doc.get("errors", []):
            errs[e["course"]] = e["error"]

    # 1) plain aggregator (concurrent) for the non-browser platforms
    try:
        doc = agg_run(date, "co.json", "output/co_plain.json",
                      PLAIN, None, False, 10, None, None)
        absorb(doc)
        ran_platforms |= PLAIN
        print(f"[plain] {doc['courses_ok']}/{doc['courses_queried']} ok, "
              f"{len(doc['tee_times'])} times, {len(doc['errors'])} errors")
    except Exception as e:  # noqa: BLE001
        print(f"[plain] FETCHER CRASHED: {type(e).__name__}: {e}")

    # 2) browser fetchers (single-date run signature)
    for platform, mod in BROWSER.items():
        if not any(c["platform"] == platform for c in co):
            continue
        try:
            m = __import__(f"scraper.{mod}", fromlist=["run"])
            doc = m.run(date, "co.json", f"output/co_{platform}.json")
            absorb(doc)
            ran_platforms.add(platform)
            print(f"[{platform}] {doc['courses_ok']}/{doc['courses_queried']} ok, "
                  f"{len(doc['tee_times'])} times, {len(doc['errors'])} errors")
        except Exception as e:  # noqa: BLE001
            print(f"[{platform}] FETCHER CRASHED: {type(e).__name__}: {e}")

    # 3) clubcaddie (its run() takes a list of dates + out-dir)
    if any(c["platform"] == "clubcaddie" for c in co):
        try:
            from scraper import browser_clubcaddie as bcc
            bcc.run([date], "co.json", "output")
            doc = json.load(open(f"output/cc_{date}.json"))
            absorb(doc)
            ran_platforms.add("clubcaddie")
            print(f"[clubcaddie] {doc['courses_ok']}/{doc['courses_queried']} ok, "
                  f"{len(doc['tee_times'])} times, {len(doc['errors'])} errors")
        except Exception as e:  # noqa: BLE001
            print(f"[clubcaddie] FETCHER CRASHED: {type(e).__name__}: {e}")

    # ---- per-course report, grouped by platform ----
    print("\n" + "=" * 72)
    by_platform: dict[str, list] = collections.defaultdict(list)
    for c in co:
        by_platform[c["platform"]].append(c)

    problems = []
    for platform in sorted(by_platform):
        rows = sorted(by_platform[platform], key=lambda c: c["slug"])
        print(f"\n## {platform}  ({len(rows)} sources)")
        for c in rows:
            slug = c["slug"]
            n = counts.get(slug, 0)
            err = errs.get(slug)
            if platform not in ran_platforms and platform.startswith("other:"):
                note = "not-run (unsupported platform)"
            elif err:
                note = f"ERROR: {err}"
                problems.append((slug, platform, note))
            elif n == 0:
                note = "0 — EMPTY (no error)"
                problems.append((slug, platform, note))
            else:
                note = f"{n} times"
            role = "" if c.get("source_role", "primary") == "primary" else " [suppl]"
            print(f"  {slug:44}{role:8} {note}")

    print("\n" + "=" * 72)
    print(f"SUMMARY: {len(co)} CO sources | "
          f"{sum(1 for c in co if counts.get(c['slug'],0)>0)} capturing | "
          f"{sum(1 for c in co if counts.get(c['slug'],0)==0 and c['slug'] in errs)} errored | "
          f"{sum(1 for c in co if counts.get(c['slug'],0)==0 and c['slug'] not in errs)} empty")
    print(f"\nTotal CO tee times captured: {sum(counts.values())}")
    if problems:
        print(f"\n--- {len(problems)} sources needing attention ---")
        for slug, platform, note in problems:
            print(f"  {slug:44} ({platform}) {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
