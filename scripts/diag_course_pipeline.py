"""Trace one course end to end: registry -> fetch -> D1 -> what the API serves.

WHY
---
`trinidad-golf-course` is the case this was written for, and it is a shape the
existing probes could not resolve. Every stage looked fine in isolation:

  registry    present, platform teeitup, status ready, alias resolves
  fetch       probe-results/teeitup-co.json — 47 rows on today+1, 46 on
              today+3, through the REAL fetch_course() the hourly scan uses
  D1          /api/tee-times?course=trinidad-golf-course&include_past=1
              answers count=0. Not stale, not deactivated. Nothing, ever.

A course that fetches and does not land is a break BETWEEN stages, and no
probe we had looked at two stages in the same run. probe_newly_ready asks
whether the fetch works. probe_staleness asks whether D1 disagrees with the
platform for rows that already exist. Neither asks the question that matters
here: the fetch works, so where do the rows go?

WHAT IT DOES
------------
Runs the real production path — the same `fetch_course()` the hourly scan
calls and the same `sync()` the push calls — for a named handful of courses,
over the same date window the scan uses (scraper.dates, per-timezone local
today + N, NOT `date -u`). Then it reads D1 back for those slugs and prints
what actually landed.

It reads D1 BEFORE fetching as well, so the report is a before/after and not
a snapshot: "0 rows before, 47 fetched, 47 after" and "0 before, 47 fetched,
0 after" are completely different findings, and only the second one is a
sync bug.

  --push is OFF by default. Without it this is read-only and answers "would
  these rows land"; with it, it performs the same write the hourly scan
  performs — no new risk class, and for a course the scan is somehow missing
  it is also the repair.

READING IT
----------
  fetched > 0, after == fetched   the pipeline works; the scan is not
                                  reaching this course (window, shard,
                                  exclude list, or the scan is failing)
  fetched > 0, after == 0         sync() dropped them. A real bug.
  already_current                 rows present, sync wrote nothing. NOT a
                                  failure — sync() only writes a row that
                                  moved, so a second run minutes later is
                                  correctly a no-op.
  fetched == 0                    the platform has no sheet for these dates.
                                  Compare the dates: the scan's window is
                                  local today + 2, and a course whose sheet
                                  opens further out has nothing to give it.
  error                           recorded verbatim, never inferred from and
                                  never folded into "empty" — the failure
                                  mode this repo keeps re-learning.

  python3 scripts/diag_course_pipeline.py --courses trinidad-golf-course
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.aggregate import fetch_course, load_registry  # noqa: E402
from scraper.d1 import D1Rest, HttpBackend, SqliteLocal, sync  # noqa: E402
from scraper.dates import scrape_dates  # noqa: E402


def d1_state(db, slugs: list[str]) -> dict:
    """What D1 holds for these slugs right now. Includes inactive rows —
    'deactivated' and 'never written' are different diagnoses."""
    if not slugs:
        return {}
    ph = ",".join("?" * len(slugs))
    rows = db.execute(
        "SELECT course_slug, COUNT(*) AS n, "
        "SUM(active) AS n_active, MAX(state) AS state, MAX(venue_id) AS venue_id, "
        "MIN(teetime) AS first_tt, MAX(teetime) AS last_tt, "
        "MAX(last_seen_at) AS newest "
        f"FROM tee_times WHERE course_slug IN ({ph}) GROUP BY course_slug",
        list(slugs))
    return {r["course_slug"]: r for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--courses", required=True,
                    help="comma-separated course slugs")
    ap.add_argument("--days", type=int, default=3,
                    help="same window the hourly scan uses (local today + N-1)")
    ap.add_argument("--push", action="store_true",
                    help="sync the fetched rows into D1 — the same write the "
                         "hourly scan performs. Off by default")
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--out", default="probe-results/course-pipeline.json")
    ap.add_argument("--local", metavar="SQLITE_FILE")
    a = ap.parse_args()

    slugs = [s.strip() for s in a.courses.split(",") if s.strip()]
    reg = {c["slug"]: c for c in load_registry(a.registry)}
    if a.local:
        db = SqliteLocal(a.local)
    elif os.environ.get("VPS_ONLY") == "1":
        # Same cutover switch as scraper.d1's CLI: the VPS Postgres is the
        # sole store, D1 is never touched. Without this the diag trace kept
        # writing the retired D1 while the site read the VPS.
        db = HttpBackend()
        print("VPS-ONLY backend (D1 disabled)", file=sys.stderr)
    else:
        db = D1Rest()
    dates = scrape_dates(a.registry, a.days)

    print("diag_course_pipeline: registry -> fetch -> D1, in one run")
    print(f"courses: {', '.join(slugs)}")
    print(f"dates:   {', '.join(d.isoformat() for d in dates)}  "
          f"(the hourly scan's window: local today + {a.days - 1})")
    print(f"push:    {a.push}\n")

    before = d1_state(db, slugs)
    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "dates": [d.isoformat() for d in dates],
                 "push": a.push, "courses": []}

    for slug in slugs:
        rec: dict = {"slug": slug}
        course = reg.get(slug)
        b = before.get(slug)
        rec["d1_before"] = (dict(b) if b else None)
        print("=" * 72)
        print(slug)
        print("=" * 72)
        if b:
            print(f"  D1 before: {b['n']} rows ({b['n_active']} active)  "
                  f"state={b['state']} venue={b['venue_id']}  "
                  f"{b['first_tt']} .. {b['last_tt']}  newest={b['newest']}")
        else:
            print("  D1 before: NO ROWS AT ALL (not stale, not deactivated — "
                  "never written)")

        if course is None:
            rec["error"] = "slug not in registry"
            print("  registry: MISSING — nothing downstream can work")
            out["courses"].append(rec)
            continue
        print(f"  registry: platform={course['platform']} "
              f"status={course.get('status')} ids={course.get('ids')}")

        # --- fetch, per date, through the production path ------------------
        per_date, total, fetched_docs = {}, 0, []
        for d in dates:
            res = fetch_course(course, d)
            if res.ok:
                per_date[d.isoformat()] = {"result": "rows" if res.tee_times
                                           else "empty",
                                           "slots": len(res.tee_times)}
                total += len(res.tee_times)
                fetched_docs.append((d, res.tee_times))
            else:
                per_date[d.isoformat()] = {"result": "error",
                                           "error": str(res.error)[:300]}
            r = per_date[d.isoformat()]
            print(f"  fetch {d.isoformat()}  {r['result']:<6} "
                  f"{r.get('slots', 0):>4} slots"
                  + (f"   {r['error'][:140]}" if r.get("error") else ""))
            sys.stdout.flush()
        rec["fetch"] = per_date
        rec["fetched_total"] = total

        # --- push, one document per date, exactly as the scan does ---------
        if a.push and total:
            rec["sync"] = {}
            for d, tts in fetched_docs:
                if not tts:
                    continue
                # Same document shape aggregate.run() writes. generated_at,
                # courses_queried and courses_ok are not decoration — sync()
                # indexes them directly to write the `runs` audit row, so a
                # document missing them raises KeyError halfway through, after
                # the tee_times writes have already been applied.
                doc = {"generated_at": dt.datetime.now(dt.timezone.utc)
                       .isoformat(),
                       "date": d.isoformat(),
                       "courses_queried": 1,
                       "courses_ok": 1,
                       "tee_times": [t.to_dict(False) for t in tts],
                       "errors": []}
                s = sync(db, doc)
                rec["sync"][d.isoformat()] = s
                print(f"  sync  {d.isoformat()}  {s}")
                sys.stdout.flush()
        elif a.push:
            print("  sync  skipped — nothing fetched, and pushing an empty "
                  "document would deactivate this course's rows")

        out["courses"].append(rec)

    # --- read D1 back ------------------------------------------------------
    after = d1_state(db, slugs)
    print("\n" + "=" * 72)
    print("AFTER")
    print("=" * 72)
    for rec in out["courses"]:
        s = rec["slug"]
        af = after.get(s)
        rec["d1_after"] = dict(af) if af else None
        bn = (rec["d1_before"] or {}).get("n", 0)
        an = (af or {}).get("n", 0)
        fetched = rec.get("fetched_total", 0)

        # A no-op sync is the normal steady state, not a failure. sync() only
        # writes a row whose open_spots/price moved or that was inactive, so
        # re-running this minutes later legitimately reports
        # inserted=updated=0 with the rows sitting right there. Reading that
        # as "did not land" is the same absent-vs-known confusion the rest of
        # this file is built to avoid — so the count written is what decides,
        # and `an == 0` is the only thing that can mean nothing landed.
        wrote = sum(s.get("rows_inserted", 0) + s.get("rows_updated", 0)
                    for s in (rec.get("sync") or {}).values())

        if rec.get("error"):
            verdict = "not_in_registry"
        elif all(v.get("result") == "error" for v in rec.get("fetch", {}).values()):
            verdict = "fetch_error"
        elif fetched == 0:
            verdict = "platform_has_no_sheet_in_window"
        elif not a.push:
            verdict = ("pipeline_untested_no_push" if an == 0
                       else "already_in_d1")
        elif an == 0:
            verdict = "fetched_but_did_not_land"
        elif an > bn or wrote:
            verdict = "landed"
        else:
            verdict = "already_current"

        rec["verdict"] = verdict
        print(f"  {s:<34} before={bn:<5} fetched={fetched:<5} after={an:<5} "
              f"{verdict}")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
