"""Multi-date confirmation for the CO courses the single-date regression flagged.

Runs each flagged source through its real fetcher across several dates. A course
that returns tee times on ANY date is fine (the single-date 0 was just that day's
availability or a transient browser hiccup); a course that is 0/errored on EVERY
date is a real miss to fix. Prints a per-course matrix + verdict.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.aggregate import load_registry, run as agg_run  # noqa: E402

REG = "registry.json"
OFFSETS = [1, 2, 3, 5, 7]

PLAIN = {"foreup", "teeitup", "chronogolf", "membersports",
         "quick18", "teesnap", "foretees"}
BROWSER = {"clubprophet": "browser_cps", "ezlinks": "browser_ezlinks",
           "golfnow": "browser_golfnow", "supersaas": "browser_supersaas"}

FLAGGED = {
    "rifle-creek-golf-course", "applewood-golf-course",
    "emerald-greens-golf-club", "flatirons-golf-course",
    "green-valley-ranch-golf-club", "lake-arbor-golf-club",
    "mariana-butte-golf-course", "the-olde-course-at-loveland",
    "university-of-denver-golf-club-at-highlands-ranch", "arrowhead-golf-club",
    "ironbridge-golf-club", "meeker-golf-course",
    "clubcorp-at-black-bear-golf-club", "desert-hawk-at-pueblo-west",
    "ironbridge-golf-club-golfnow", "pelican-lakes-golf-country-club",
    "tamarack-golf-course", "walking-stick-golf-course",
    "arrowhead-golf-club-golfnow", "lincoln-park-golf-course",
    "tiara-rado-golf-course", "homestead-golf-course", "golf-granby-ranch",
    "raccoon-creek-golf-course", "rollingstone-ranch-golf-club",
    "trinidad-golf-course", "vail-golf-club", "coyote-creek-golf-course",
    "hollydot-golf-course", "the-course-at-petteys-park",
}


def main() -> int:
    reg = load_registry(REG)
    sub = [c for c in reg if c["slug"] in FLAGGED]
    json.dump({"courses": sub}, open("flagged.json", "w"))
    today = dt.date.today()
    dates = [today + dt.timedelta(days=o) for o in OFFSETS]
    print(f"=== CO empties confirmation — {len(sub)} sources x {len(dates)} dates "
          f"({dates[0]}..{dates[-1]}) ===\n")

    counts: dict = collections.defaultdict(dict)   # slug -> {date -> int}
    notes: dict = collections.defaultdict(dict)    # slug -> {date -> err str}

    def absorb(doc, date):
        for t in doc.get("tee_times", []):
            counts[t["course_slug"]][date] = counts[t["course_slug"]].get(date, 0) + 1
        for e in doc.get("errors", []):
            notes[e["course"]][date] = e["error"][:18]

    for date in dates:
        try:
            absorb(agg_run(date, "flagged.json", f"output/vp_{date}.json",
                           PLAIN, None, False, 8, None, None), date)
        except Exception as e:  # noqa: BLE001
            print(f"  [plain {date}] crashed: {type(e).__name__}: {e}")
        for platform, mod in BROWSER.items():
            if not any(c["platform"] == platform for c in sub):
                continue
            try:
                m = __import__(f"scraper.{mod}", fromlist=["run"])
                absorb(m.run(date, "flagged.json", f"output/v_{platform}_{date}.json"), date)
            except Exception as e:  # noqa: BLE001
                print(f"  [{platform} {date}] crashed: {type(e).__name__}: {e}")
        if any(c["platform"] == "clubcaddie" for c in sub):
            try:
                from scraper import browser_clubcaddie as bcc
                bcc.run([date], "flagged.json", "output")
                absorb(json.load(open(f"output/cc_{date}.json")), date)
            except Exception as e:  # noqa: BLE001
                print(f"  [clubcaddie {date}] crashed: {type(e).__name__}: {e}")

    hdr = "  ".join(d.strftime("%m-%d") for d in dates)
    print(f"{'course':44} {'platform':12} {hdr}   verdict")
    real_miss = []
    for c in sorted(sub, key=lambda c: (c["platform"], c["slug"])):
        slug = c["slug"]
        cells, got = [], False
        for d in dates:
            if d in counts.get(slug, {}):
                cells.append(f"{counts[slug][d]:>5}")
                got = True
            elif d in notes.get(slug, {}):
                cells.append(f"{'E':>5}")
            else:
                cells.append(f"{0:>5}")
        verdict = "ok (date-specific)" if got else "*** REAL MISS ***"
        if not got:
            real_miss.append((slug, c["platform"]))
        print(f"{slug:44} {c['platform']:12} {'  '.join(cells)}   {verdict}")

    print(f"\n{len(real_miss)} sources captured NOTHING across all {len(dates)} dates:")
    for slug, p in real_miss:
        print(f"  {slug:44} ({p})")
    print("\n(E = errored that date; a number = tee times captured)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
