"""Compare two state_status.py reports and fail if a state went backwards.

The alarm that has to stay readable at fifty states. It compares ONE number
per state — live venues — so a bad deploy reads as "AZ 133 -> 112" rather than
twenty course names, and the blocker table in the committed report says which
platform to look at.

Asymmetric on purpose: a drop beyond --tolerance exits 1, a rise never does.
Coverage going up does not need a human. Tolerance exists because a handful of
courses legitimately go quiet overnight (seasonal closure, a course that only
opens its sheet 48h out), and an alarm that cries wolf gets muted, which is
worse than no alarm.

A state that appears for the first time is not a regression. A state that
DISAPPEARS is — that means its CSV stopped being read.

  python3 scripts/state_regression.py OLD.json NEW.json --tolerance 3
"""
from __future__ import annotations

import argparse
import json
import sys


def live_by_state(path: str) -> dict[str, int]:
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {s["state"]: s["counts"]["live"] for s in doc.get("states", [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("previous")
    ap.add_argument("current")
    ap.add_argument("--tolerance", type=int, default=3,
                    help="live venues a state may lose without failing")
    a = ap.parse_args()

    old, new = live_by_state(a.previous), live_by_state(a.current)
    if not old:
        print("no previous run to compare against — baseline recorded")
        return 0
    if not new:
        print("FAIL: current report has no states at all")
        return 1

    problems, moves = [], []
    for st in sorted(set(old) | set(new)):
        b, n = old.get(st), new.get(st)
        if b is None:
            moves.append(f"  {st}  new  -> {n} live")
            continue
        if n is None:
            problems.append(f"  {st}  DISAPPEARED (was {b} live) — its source "
                            f"CSV is no longer being read")
            continue
        d = n - b
        if d < -a.tolerance:
            problems.append(f"  {st}  {b} -> {n} live  ({d})")
        elif d:
            moves.append(f"  {st}  {b} -> {n} live  ({d:+d})")

    for line in moves:
        print(line)
    if not problems:
        print(f"OK — no state lost more than {a.tolerance} live courses")
        return 0

    print(f"\nREGRESSION — {len(problems)} state(s) went backwards:")
    for line in problems:
        print(line)
    print("\nSee probe-results/state-status.txt: the blocker table groups the "
          "loss by booking platform, which is where the fix is.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
