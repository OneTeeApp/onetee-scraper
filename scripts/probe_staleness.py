"""How much of the active inventory is orphaned — served, but no longer real?

WHY
---
`sync()` only deactivates rows for courses that reported in THAT run:

    to_deactivate = [k for k, e in existing.items()
                     if e["active"] and k not in scraped
                     and k[0] in scraped_courses and k[0] not in errored]

`k[0] in scraped_courses` is the load-bearing clause. A course that reports is
fully reconciled — slots that vanished get closed out correctly. A course that
reports NOTHING never enters `scraped_courses` at all, so its last-known rows
stay active=1 until each one individually elapses via prune_past. Between the
moment a course goes dark and the moment its slots age out, we serve tee times
that no longer exist. The comment at d1.py:208-214 already names this.

WHY NOT JUST READ last_seen_at
------------------------------
Because it does not mean what its name suggests. sync() bumps it on INSERT and
on UPDATE, and a row is only UPDATEd when open_spots or price actually moved:

    if e and (e["open_spots"] != v["open_spots"] or ... or not e["active"])

A slot that is re-scraped every hour and comes back identical is in neither
list, so its timestamp never moves. That is a deliberate free-tier write
saving, not a bug — but it makes an old last_seen_at ambiguous between:

    "this course went dark N hours ago"          <- orphaned, must be closed
    "this slot has sat unbooked and unchanged"   <- healthy, must be left alone

Deactivating on age alone would delete real inventory at quiet courses. So the
timestamp is used ONLY to narrow the candidate set, and every candidate is then
adjudicated by running the real fetch path against it. The fetch is what
decides; the timestamp just keeps us from fetching all ~600 courses.

VERDICTS
--------
Course-level, because course-level is the granularity the bug lives at — a
course that reports at all is reconciled correctly by sync().

  live        the fetch returned rows. The course is up; the old timestamp
              just means stable inventory. NOT stale. Leave it alone.
  orphaned    every date fetched cleanly and returned zero rows, yet we are
              serving N active rows for it. This is the bug, measured.
  error       the fetch raised. Unknown, NOT orphaned — an adapter breaking is
              not evidence a course went dark, and inferring one from the
              other is how you delete a working course's inventory.
  unregistered the slug has left registry.json. deactivate_unknown_slugs()
              in the hourly prune already owns this case; counted, not judged.

Read-only: SELECTs against D1, public GETs to the platforms, no writes.

  python3 scripts/probe_staleness.py [--max-age-hours 6] [--local test.db]
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
from scraper.d1 import D1Rest, SqliteLocal  # noqa: E402

# Every platform family has an hourly writer of its own (scrape :17,
# clubcaddie :27, cps/supersaas :37, ezlinks :47, golfnow :52), and scrape-fast
# runs every 5 minutes, so under normal operation nothing should be more than
# ~1-2h old. 6h is deliberately loose: Actions cron drifts under load, and the
# cost of a generous threshold is a few extra fetches, while the cost of a
# tight one is noise that buries the real orphans.
DEFAULT_MAX_AGE_H = 6

# Adjudicate against the dates we are actually serving for that course, capped
# so one course with a long window cannot dominate the run.
MAX_DATES = 3


def _age_hours(ts: str, now: dt.datetime) -> float | None:
    """Hours between an ISO timestamp and now. None if it will not parse —
    an unparseable timestamp is unknown, not old."""
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--out", default="probe-results/staleness.json")
    ap.add_argument("--local", metavar="SQLITE_FILE")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_H)
    ap.add_argument("--state", help="limit to one state (e.g. CO)")
    ap.add_argument("--no-adjudicate", action="store_true",
                    help="measure the distribution only; do not fetch")
    a = ap.parse_args()

    db = SqliteLocal(a.local) if a.local else D1Rest()
    now = dt.datetime.now(dt.timezone.utc)

    where = "active = 1"
    params: list = []
    if a.state:
        where += " AND state = ?"
        params.append(a.state)

    rows = db.execute(
        "SELECT course_slug, MAX(state) AS state, MAX(platform) AS platform, "
        "COUNT(*) AS n, MAX(last_seen_at) AS newest, MIN(teetime) AS first_tt, "
        "MAX(teetime) AS last_tt "
        f"FROM tee_times WHERE {where} GROUP BY course_slug", params)

    total_rows = sum(r["n"] for r in rows)
    print(f"{len(rows)} courses with active inventory, "
          f"{total_rows} active rows"
          + (f" (state={a.state})" if a.state else ""), flush=True)

    for r in rows:
        r["age_hours"] = _age_hours(r["newest"], now)

    # Distribution first — this is the part that is true regardless of what the
    # fetches say, and it is what tells us whether the threshold is sane.
    def bucket(h: float | None) -> str:
        if h is None:
            return "unparseable"
        for edge, name in ((2, "<2h"), (6, "2-6h"), (24, "6-24h"),
                           (72, "1-3d")):
            if h < edge:
                return name
        return ">3d"

    dist = Counter(bucket(r["age_hours"]) for r in rows)
    rows_by_bucket: Counter = Counter()
    for r in rows:
        rows_by_bucket[bucket(r["age_hours"])] += r["n"]
    print("\nage of newest last_seen_at, per course (rows in parens):")
    for name in ("<2h", "2-6h", "6-24h", "1-3d", ">3d", "unparseable"):
        if dist.get(name):
            print(f"  {name:<12} {dist[name]:>4} courses "
                  f"({rows_by_bucket[name]} rows)")

    candidates = [r for r in rows
                  if r["age_hours"] is not None
                  and r["age_hours"] >= a.max_age_hours]
    candidates.sort(key=lambda r: -r["age_hours"])
    print(f"\n{len(candidates)} candidates older than {a.max_age_hours}h "
          f"({sum(c['n'] for c in candidates)} rows)", flush=True)

    reg = {c["slug"]: c for c in load_registry(a.registry)}

    results = []
    for c in candidates:
        slug = c["course_slug"]
        row = {"slug": slug, "state": c["state"], "platform": c["platform"],
               "active_rows": c["n"], "newest_last_seen": c["newest"],
               "age_hours": round(c["age_hours"], 1),
               "teetime_range": [c["first_tt"], c["last_tt"]]}

        course = reg.get(slug)
        if course is None:
            row["verdict"] = "unregistered"
            results.append(row)
            print(f"  {slug:<46} unregistered   {c['n']} rows", flush=True)
            continue

        if a.no_adjudicate:
            row["verdict"] = "not_adjudicated"
            results.append(row)
            continue

        # The dates we are actually serving for this course. Adjudicating on
        # some other date would answer a question nobody asked.
        dates = sorted({t[:10] for t in (c["first_tt"], c["last_tt"]) if t})
        today = str(dt.date.today())
        dates = [d for d in dates if d >= today][:MAX_DATES] or [today]

        per_date, errors, total = {}, [], 0
        for d in dates:
            res = fetch_course(course, dt.date.fromisoformat(d))
            if res.ok:
                per_date[d] = len(res.tee_times)
                total += len(res.tee_times)
            else:
                per_date[d] = None
                errors.append(f"{d}: {res.error}"[:300])

        if total:
            verdict = "live"
        elif len(errors) == len(dates):
            verdict = "error"
        elif errors:
            verdict = "partial_error"
        else:
            verdict = "orphaned"

        row["verdict"] = verdict
        row["fetched"] = per_date
        row["fetched_rows"] = total
        if errors:
            row["errors"] = errors
        results.append(row)
        print(f"  {slug:<46} {verdict:<14} serving {c['n']:>4} rows, "
              f"fetch returned {total}", flush=True)
        if errors:
            print(f"      {errors[0]}", flush=True)

    tally = Counter(r["verdict"] for r in results)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    orphan_rows = sum(r["active_rows"] for r in results
                      if r["verdict"] == "orphaned")
    pct = f" ({orphan_rows / total_rows * 100:.1f}% of active inventory)" \
        if total_rows else ""
    print(f"\nORPHANED: {tally.get('orphaned', 0)} courses, {orphan_rows} rows "
          f"served as current with nothing behind them{pct}")
    for slug in [r["slug"] for r in results if r["verdict"] == "orphaned"]:
        print(f"  {slug}")

    out = {"generated_at": now.isoformat(timespec="seconds"),
           "max_age_hours": a.max_age_hours,
           "state": a.state,
           "courses_with_active_rows": len(rows),
           "active_rows": total_rows,
           "age_distribution": {k: {"courses": dist[k],
                                    "rows": rows_by_bucket[k]}
                                for k in dist},
           "tally": dict(tally),
           "orphaned_rows": orphan_rows,
           "results": results}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
