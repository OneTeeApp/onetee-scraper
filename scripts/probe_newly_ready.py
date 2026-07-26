"""Do the newly-unheld courses actually return tee times?

WHY
---
e14376c flipped 32 courses from `needs_ids` to `ready` on the strength of
probe-results/needs-ids.json, which asked each platform whether it could
resolve the identifiers the adapters need. It could. That is not the same
claim as "this course returns tee times":

  discover() succeeding  = the platform knows this club and will hand over a
                           club_id / affiliation_type / course_ids, or a
                           schedule_id.
  fetch() succeeding     = the tee-sheet call built from those ids comes back
                           with rows.

Everything between those two — the marketplace query 422ing on an affiliation
we resolved but may not be allowed to use, a schedule that exists but is
closed for the season, a course_id that resolves and then serves an empty
sheet forever — lives in the gap. So this runs the REAL fetch path, the same
`fetch_course()` the hourly scan calls, against the same courses.

READING THE RESULT
------------------
The three outcomes are kept apart on purpose, because collapsing them is the
bug this repo keeps re-learning (the GolfNow silent-empties, #72's verdict()
fix). An error is not an empty sheet, and an empty sheet on every date is not
the same as an empty sheet on one:

  rows      at least one date returned tee times. The unhold was right.
  empty     every date fetched cleanly and returned zero rows. NOT a pass and
            NOT a failure — a course can be genuinely closed, seasonal, or
            fully booked. It is the bucket that needs a human to look, and it
            is exactly what a silent breakage looks like, so it is never
            folded into either of the others.
  error     the fetch raised. The message is recorded verbatim; nothing is
            inferred from it.

Three dates rather than one, spread out, so a single closed day or a
tournament does not read as a dead course.

Report only: no D1 writes, no CSV edits, public endpoints, GET only.

  python3 scripts/probe_newly_ready.py [--out probe-results/newly-ready.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.aggregate import fetch_course, load_registry  # noqa: E402

# Orange Tree was not in the needs_ids probe — it was `ready` all along, but
# pointed at ForeUp booking id 21561, which is Legend Trail's. e14376c moved it
# to quick18, so it is an untested code path for this course too.
EXTRA = {"orange-tree-golf-resort"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--probe", default="probe-results/needs-ids.json")
    ap.add_argument("--out", default="probe-results/newly-ready.json")
    ap.add_argument("--days", default="1,4,9")
    a = ap.parse_args()

    dates = [dt.date.today() + dt.timedelta(days=int(d))
             for d in a.days.split(",") if d.strip()]

    with open(a.probe) as fh:
        probed = {r["slug"] for r in json.load(fh)["results"]}

    reg = load_registry(a.registry)
    # The unheld set: rows that were in the needs_ids probe and are `ready`
    # now. Derived rather than hardcoded, so this stays honest if the gate or
    # the holds change under it.
    targets = [c for c in reg
               if c["status"] == "ready"
               and (c["slug"] in probed or c["slug"] in EXTRA)]

    print(f"{len(targets)} newly-ready courses x {len(dates)} dates "
          f"({', '.join(str(d) for d in dates)})\n", flush=True)

    results = []
    for c in targets:
        per_date, errors, total = {}, [], 0
        for d in dates:
            res = fetch_course(c, d)
            if res.ok:
                per_date[str(d)] = len(res.tee_times)
                total += len(res.tee_times)
            else:
                per_date[str(d)] = None
                errors.append(f"{d}: {res.error}"[:300])

        if total:
            verdict = "rows"
        elif errors and len(errors) == len(dates):
            verdict = "error"
        elif errors:
            # Some dates raised and the rest came back empty. Not "rows", and
            # calling it "empty" would bury a real exception, so it keeps its
            # own name.
            verdict = "partial_error"
        else:
            verdict = "empty"

        row = {"slug": c["slug"], "state": c.get("state"),
               "platform": c["platform"], "verdict": verdict,
               "total_rows": total, "by_date": per_date}
        if errors:
            row["errors"] = errors
        results.append(row)
        print(f"{c.get('state')} {c['platform']:<11} {c['slug']:<44} "
              f"{verdict:<14} {total} rows", flush=True)
        if errors:
            print(f"      {errors[0]}", flush=True)

    tally = Counter(r["verdict"] for r in results)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    for v in ("empty", "error", "partial_error"):
        bad = [r["slug"] for r in results if r["verdict"] == v]
        if bad:
            print(f"  {v}: " + ", ".join(bad))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump({"dates": [str(d) for d in dates], "probed": len(results),
                   "tally": dict(tally), "results": results}, fh, indent=1)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
