"""Track what the frontend is actually serving, hour by hour, per state.

WHY
---
`probe_staleness.py --deactivate` now sets active=0 on courses it has proved
are dark. That is a write against live inventory, so it needs a witness: a
number recorded before and after, on a fixed cadence, that says whether the
sweep is trimming what no longer exists or eating real coverage.

WHAT IT MEASURES, AND WHY NOT D1
--------------------------------
It reads the Worker API, not D1. The question is "what does a golfer see for
Colorado right now", and only the API answers that — it applies the per-state
local-time cutoff, dedupes a venue's native + supplement sources, and groups by
venue_id. Re-deriving all of that in a SELECT here would mean maintaining a
second copy of the worker's timezone table and its DISTINCT key, and the copy
would drift. The API is also reachable without the Cloudflare secrets, so this
job holds no credentials and can write nothing.

WHEN
----
:57, after every hourly writer has run (scrape :17, prune :23, clubcaddie :27,
cps/supersaas :37, ezlinks :47, golfnow :52) and before the next sweep at :07.

That offset is deliberate. It measures the DURABLE effect, not the momentary
one. sync() reactivates on `not e["active"]`, so a course the sweep closed out
wrongly is re-INSERTed by the next successful scrape and is back by :57. A
course it closed out correctly stays gone. Sampling at :12, right after the
sweep, would show every close-out as a loss and could not tell those apart.

READING IT
----------
Total slots move on their own all day: slots elapse, bookings take them, and a
new day's sheet appears at the far end of the window. So a slot delta alone
proves little. The signal that matches what the sweep actually does — it acts
on whole courses — is a VENUE going from serving to silent:

  gone       served slots last sample, serves none now. This is what a
             close-out looks like. One or two is the sweep working. A cluster
             on one platform is a platform outage being read as death.
  returned   silent last sample, serving now. Expected, and the reason the
             sweep is safe to be wrong in one direction.

A second state (AZ by default) is sampled as a control. Colorado moving while
the control stays flat is the sweep. Both moving together is something else —
a scrape failure, an API problem, a deploy.

Append-only: one JSON line per run in a file git keeps forever, so the series
is the history. Read-only over HTTP; no D1, no writes, no credentials.

  python3 scripts/monitor_inventory.py [--states CO,AZ]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import requests

DEFAULT_API = "https://api.oneteeapp.com"

# A venue count that swings by more than this between two samples is worth a
# human look. Deliberately not a slot threshold: slot counts breathe hourly on
# their own, venue counts do not.
GONE_ALERT = 3
# Slots are informational, but a collapse is a collapse.
SLOT_ALERT_PCT = 15.0


def fetch_state(api: str, state: str) -> dict:
    """Venue -> slot count for one state, as the frontend sees it.

    Raises on anything that is not a clean answer. A failed sample must not be
    recorded as an empty one — that is the bug this repo keeps re-learning, and
    here it would read as every course in the state going dark at once.
    """
    r = requests.get(f"{api}/api/courses", params={"state": state}, timeout=60)
    r.raise_for_status()
    courses = r.json()["courses"]
    return {c["course_slug"]: {"slots": c.get("slots") or 0,
                               "name": c.get("course_name"),
                               "platform": c.get("platform")}
            for c in courses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("ONETEE_API", DEFAULT_API))
    ap.add_argument("--states", default="CO,AZ,VA,FL",
                    help="first is the watched state; the rest are controls")
    ap.add_argument("--history", default="probe-results/inventory-history.jsonl")
    a = ap.parse_args()

    states = [s.strip().upper() for s in a.states.split(",") if s.strip()]
    # The workflow pins --states to its own fallback list, which went stale
    # the day VA launched and stayed stale through FL: the monitor simply
    # never sampled the newest states. Rather than keeping two hardcoded
    # lists in step (workflow fallback + this default), union the requested
    # states with every state the registry actually covers — the first
    # requested state stays the watched one, and a new state starts being
    # sampled the moment its CSV lands in SOURCES, with no workflow edit
    # (which a contents-scoped PAT cannot push anyway).
    try:
        with open("registry.json") as fh:
            reg_states = {c.get("state") for c in json.load(fh)["courses"]}
        states += sorted(s for s in reg_states if s and s not in states)
    except (OSError, ValueError, KeyError):
        pass                     # no registry checkout — sample what was asked
    now = dt.datetime.now(dt.timezone.utc)

    sample: dict = {"generated_at": now.isoformat(timespec="seconds"),
                    "source": "api",
                    "states": {}}

    try:
        h = requests.get(f"{a.api}/api/health", timeout=30).json()
        sample["health"] = {"total": h.get("total"), "active": h.get("active")}
    except Exception as e:                                   # noqa: BLE001
        # The health counter is context, not the measurement. Losing it must
        # not lose the sample.
        sample["health_error"] = str(e)[:200]

    per_state_courses: dict[str, dict] = {}
    for st in states:
        courses = fetch_state(a.api, st)
        per_state_courses[st] = courses
        serving = {k: v for k, v in courses.items() if v["slots"] > 0}
        sample["states"][st] = {
            "venues_serving": len(serving),
            "slots": sum(v["slots"] for v in serving.values()),
            "courses": {k: v["slots"] for k, v in sorted(serving.items())},
        }

    # --- compare against the previous sample --------------------------------
    prev = None
    if os.path.exists(a.history):
        with open(a.history) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        if lines:
            prev = json.loads(lines[-1])

    # How far apart these two samples ACTUALLY are. The :57 cron is the
    # intent, not the guarantee — GitHub drops and delays scheduled runs, and
    # this series has already skipped hours. Both thresholds below are
    # calibrated for roughly hourly spacing: GONE_ALERT counts venues lost
    # "in one hour", and a 15% slot drop is alarming over an hour and
    # unremarkable overnight. Comparing a 26-hour gap against them silently
    # reports normal daily churn as a sweep failure, so record the real gap
    # and say so rather than letting the reader assume the cadence held.
    gap_h = None
    if prev:
        try:
            gap_h = (now - dt.datetime.fromisoformat(prev["generated_at"])
                     ).total_seconds() / 3600.0
            sample["hours_since_prev"] = round(gap_h, 2)
        except (ValueError, KeyError):
            pass

    print(f"sample {sample['generated_at']}  api={a.api}")
    if gap_h is not None:
        note = "" if gap_h <= 3 else "   [thresholds assume ~1h spacing]"
        print(f"  {gap_h:.1f}h since the previous sample{note}")
    if sample.get("health"):
        print(f"  health: total={sample['health']['total']} "
              f"active={sample['health']['active']}")

    alerts: list[str] = []
    for st in states:
        cur = sample["states"][st]
        tag = "WATCHED" if st == states[0] else "control"
        line = (f"\n{st} ({tag}): {cur['venues_serving']} venues serving, "
                f"{cur['slots']} upcoming slots")
        if not prev or st not in prev.get("states", {}):
            print(line + "   [no previous sample to compare]")
            continue

        p = prev["states"][st]
        d_v = cur["venues_serving"] - p["venues_serving"]
        d_s = cur["slots"] - p["slots"]
        pct = (d_s / p["slots"] * 100) if p["slots"] else 0.0
        span = f" over {gap_h:.1f}h" if gap_h is not None else ""
        print(line + f"   ({d_v:+d} venues, {d_s:+d} slots, {pct:+.1f}%){span} "
                     f"vs {prev['generated_at']}")

        if pct <= -SLOT_ALERT_PCT:
            alerts.append(f"{st}: slots fell {pct:.1f}%{span} "
                          f"({p['slots']} -> {cur['slots']})")

        # The seeded pre-sweep baseline carries totals but no per-course map —
        # it was recorded by hand before this job existed. Diffing against an
        # absent map would report every venue in the state as newly returned.
        if not p.get("courses"):
            print("    [previous sample has totals only; no per-venue diff]")
            continue

        gone = sorted(set(p["courses"]) - set(cur["courses"]))
        back = sorted(set(cur["courses"]) - set(p["courses"]))
        for slug in gone:
            print(f"    gone      {slug:<46} was serving {p['courses'][slug]}")
        for slug in back:
            print(f"    returned  {slug:<46} now serving {cur['courses'][slug]}")

        if len(gone) >= GONE_ALERT:
            alerts.append(f"{st}: {len(gone)} venues went silent"
                          f"{span or ' since the last sample'} "
                          f"({', '.join(gone[:8])})")

    if alerts:
        print("\n!! ALERT")
        for al in alerts:
            print(f"  {al}")
        sample["alerts"] = alerts
    else:
        print("\nno alert thresholds crossed")

    os.makedirs(os.path.dirname(a.history) or ".", exist_ok=True)
    with open(a.history, "a") as fh:
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
    print(f"\nappended to {a.history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
