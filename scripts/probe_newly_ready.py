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
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.aggregate import fetch_course, load_registry  # noqa: E402

# Orange Tree was not in the needs_ids probe — it was `ready` all along, but
# pointed at ForeUp booking id 21561, which is Legend Trail's. e14376c moved it
# to quick18, so it is an untested code path for this course too.
EXTRA = {"orange-tree-golf-resort"}

# The hourly monitor's newest sample: which venues the public API is actually
# serving. Crossing it with the registry gives the SILENT cohort — rows the
# registry calls ready (or experimental) that serve nothing — which is the same
# question this script already asks, just arrived at from the other direction.
# The unheld cohort asks "we said these can scrape; can they?"; the silent
# cohort asks "these should be scraping; why aren't they?" Both are answered by
# running the real fetch path, so both belong here rather than in a second
# script needing a second workflow.
INVENTORY = "probe-results/inventory-history.jsonl"


def silent_cohort(reg: list[dict], path: str) -> tuple[set[str], str]:
    """Registry rows that should be serving and are not, per the newest sample.

    Silence is read from the sample rather than pasted in, so this re-aims
    itself every run; a course that started serving since the last sample just
    drops out. Returns an empty set and a reason when the sample is unreadable,
    because probing every ready course in the fleet as a "fallback" would be a
    far bigger request storm than the thing it was meant to diagnose.
    """
    try:
        with open(path) as fh:
            last = [ln for ln in fh if ln.strip()][-1]
        sample = json.loads(last)
    except (OSError, ValueError, IndexError) as e:
        return set(), f"inventory unreadable ({type(e).__name__}) — silent cohort skipped"
    serving: set[str] = set()
    for st in sample.get("states", {}).values():
        serving |= set(st.get("courses") or {})
    if not serving:
        return set(), "inventory sample lists no serving venues — silent cohort skipped"
    silent = {c["venue_id"] for c in reg
              if c.get("source_role") == "primary"
              and c.get("status") in ("ready", "experimental")
              and c.get("venue_id") not in serving}
    return silent, f"{len(silent)} silent per sample {sample.get('generated_at')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--probe", default="probe-results/needs-ids.json")
    ap.add_argument("--out", default="probe-results/newly-ready.json")
    ap.add_argument("--days", default="1,4,9")
    ap.add_argument("--inventory", default=INVENTORY)
    ap.add_argument("--no-silent", action="store_true",
                    help="probe only the unheld cohort, as before")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the target count (0 = no cap); a cap is logged")
    ap.add_argument("--budget-minutes", type=float, default=20.0,
                    help="stop cleanly after this long and report what was "
                         "covered. Must stay under the workflow's step timeout "
                         "(25 min) or the step is killed instead of finishing.")
    a = ap.parse_args()

    dates = [dt.date.today() + dt.timedelta(days=int(d))
             for d in a.days.split(",") if d.strip()]

    with open(a.probe) as fh:
        probed = {r["slug"] for r in json.load(fh)["results"]}

    reg = load_registry(a.registry)
    # The unheld set: rows that were in the needs_ids probe and are `ready`
    # now. Derived rather than hardcoded, so this stays honest if the gate or
    # the holds change under it.
    unheld = {c["slug"] for c in reg
              if c["status"] == "ready"
              and (c["slug"] in probed or c["slug"] in EXTRA)}

    silent, why = (set(), "silent cohort disabled (--no-silent)")
    if not a.no_silent:
        silent, why = silent_cohort(reg, a.inventory)
    print(f"unheld cohort: {len(unheld)}   silent cohort: {why}", flush=True)

    chosen = unheld | silent
    targets = [c for c in reg if c["slug"] in chosen
               or (c.get("source_role") == "primary"
                   and c.get("venue_id") in silent)]
    # Deduplicate while keeping registry order.
    seen: set[str] = set()
    targets = [c for c in targets
               if not (c["slug"] in seen or seen.add(c["slug"]))]
    # Silent cohort FIRST. If the clock runs out, the work that survives should
    # be the open question, not the unheld cohort whose answer is already known
    # from previous runs. (Tripling the target count from 34 to 118 without
    # reordering is what made the first run useless.)
    def _order(c):
        in_silent = c["slug"] in silent or c.get("venue_id") in silent
        return (0 if in_silent else 1, c["slug"])
    targets.sort(key=_order)
    if a.limit and len(targets) > a.limit:
        # A cap is a silent loss of coverage unless it is said out loud.
        print(f"NOTE: --limit {a.limit} drops {len(targets) - a.limit} targets; "
              f"they are NOT covered by this report.", flush=True)
        targets = targets[:a.limit]

    cohort_of = {c["slug"]: ("unheld+silent" if c["slug"] in unheld
                             and (c["slug"] in silent
                                  or c.get("venue_id") in silent)
                             else "unheld" if c["slug"] in unheld else "silent")
                 for c in targets}
    print(f"{len(targets)} courses x {len(dates)} dates "
          f"({', '.join(str(d) for d in dates)})\n", flush=True)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)

    def flush(done: list, unreached: list, stopped: str | None) -> None:
        """Write the report as it stands.

        Called after EVERY course, not once at the end. The first run of the
        two-cohort version died on the workflow's 25-minute step timeout with
        118 targets, and because the file was only written after the last
        course, every answer it had already obtained was thrown away — the
        workflow's own comment says the step-level timeout exists precisely so
        partial findings reach the commit, and writing only at the end defeated
        that. Rewriting a ~100KB file per course is far cheaper than losing the
        run.
        """
        tal = Counter(r["verdict"] for r in done)
        with open(a.out, "w") as fh:
            json.dump({"dates": [str(d) for d in dates],
                       "probed": len(done), "tally": dict(tal),
                       "complete": not unreached,
                       "stopped_early": stopped,
                       "not_reached": [c["slug"] for c in unreached],
                       "results": done}, fh, indent=1)

    started = time.monotonic()
    budget = a.budget_minutes * 60
    results: list = []
    stopped: str | None = None
    for i, c in enumerate(targets):
        if budget and time.monotonic() - started > budget:
            # A cap is only acceptable if it is said out loud, in the log AND
            # in the artifact, so nobody reads a partial file as a full sweep.
            stopped = (f"time budget {a.budget_minutes:g} min reached after "
                       f"{len(results)} of {len(targets)} targets")
            print(f"\nSTOPPING EARLY: {stopped}", flush=True)
            print("  NOT reached: " + ", ".join(t["slug"] for t in targets[i:]),
                  flush=True)
            flush(results, targets[i:], stopped)
            break
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
               "platform": c["platform"], "cohort": cohort_of.get(c["slug"], "?"),
               "status": c.get("status"), "ids": c.get("ids") or {},
               "verdict": verdict, "total_rows": total, "by_date": per_date}
        if errors:
            row["errors"] = errors
        results.append(row)
        flush(results, targets[i + 1:], None)
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

    if stopped:
        print(f"\nINCOMPLETE: {stopped}")
    flush(results, [] if not stopped else targets[len(results):], stopped)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
