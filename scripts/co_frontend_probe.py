"""Colorado + frontend follow-ups, measured in one run.

Three questions, all of them currently answered in the changelog by a
statement that is either stale or untested:

  A. SUB-COURSE LABELS. The changelog's open list says "Kennedy's sub-course
     labels come back empty (it reports a single course)" and "Hyland Hills
     had no upcoming rows at probe time, so its sub-course display names are
     untested live". Both predate the MemberSports config sweep: Kennedy now
     carries config_ids [0,1,2] and experimental.py sweeps them, labelling
     only when a day spans more than one sub-course name. diag.txt section B
     already shows Hyland Hills returning three labels off the adapter. What
     has never been checked is whether those labels survive the trip into D1
     and back out of the read API, which is what the widget actually renders.
     So: adapter -> D1 -> API, for all five sub-course venues.

  B. THE 15 ROWS WITH NO `state`. This is the stated blocker for retiring the
     widget's stateOf() city-inference hack: while any row lacks `state`, the
     widget cannot trust the field and has to keep guessing from city names.
     Nobody has yet listed WHICH rows they are. Enumerate them by slug,
     platform and city, and cross-check each against registry.json — if the
     registry has a state for that course the gap is in the write path, not
     in our source data, and the backfill is a one-liner.

  C. THE 14 CO CAPTURE MISSES, re-measured against the CURRENT registry.
     diag.txt's list is old enough that at least two rows have moved under it:
     homestead-golf-course was quick18 (0 rows from an 18kB page) and is now
     membersports club 3807 cfg [0,7], and five of the fourteen were slug
     drift rather than capture failures at all. Re-run every one of them
     through the production fetch() so the list stops carrying entries that
     are already fixed.

     Note one thing this section is NOT re-testing: diag3 section E reported
     TeeItUp facility 2485 "Indian Peaks" returning 0 slots, which looked
     like it contradicted "Indian Peaks confirmed healthy (14 times through
     production fetcher)". It does not. Our registry runs Indian Peaks on
     clubprophet (tenant indianpeaks, course_ids [10, 11]), not TeeItUp; the
     TeeItUp facility of that name is somebody else's listing. It is included
     below on its real platform to confirm that.

Public endpoints and our own D1/Worker, report only. Nothing here edits the
CSV, the registry, or D1. No credentials to third parties, no CAPTCHA
solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from scraper.aggregate import ADAPTERS  # noqa: E402
from scraper.d1 import D1Rest  # noqa: E402

API = os.environ.get("API", "https://api.oneteeapp.com")
DATES = [dt.date.today() + dt.timedelta(days=i) for i in (0, 1, 2)]

SUBCOURSE_VENUES = [
    "kennedy-golf-course",
    "the-greg-mastriona-golf-courses-at-hyland-hills",
    "fox-hollow-golf-course",
    "broken-tee-golf-course",
    "foothills-golf-course",
]

# diag.txt section C, with the five drifted slugs resolved to their real
# registry slugs so every one of the fourteen actually gets measured.
MISSES = [
    "emerald-greens-golf-club",                          # was emerald-greens-golf-course
    "lake-arbor-golf-club",                              # was lake-arbor-golf-course
    "university-of-denver-golf-club-at-highlands-ranch",
    "clubcorp-at-black-bear-golf-club",                  # was black-bear-golf-club
    "desert-hawk-at-pueblo-west",                        # was desert-hawk-golf-course
    "pelican-lakes-golf-country-club",
    "tamarack-golf-course",
    "walking-stick-golf-course",
    "homestead-golf-course",
    "golf-granby-ranch",                                 # was granby-ranch-golf-course
    "rollingstone-ranch-golf-club",
    "coyote-creek-golf-course",
    "hollydot-golf-course",
    "the-course-at-petteys-park",                        # was petteys-park-golf-course
    "indian-peaks-golf-course",                          # the diag3 section E puzzle
]


def registry() -> dict[str, dict]:
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "registry.json")) as fh:
        doc = json.load(fh)
    rows = doc["courses"] if isinstance(doc, dict) else doc
    return {c["slug"]: c for c in rows}


def api_get(path: str):
    r = requests.get(API + path, timeout=30)
    r.raise_for_status()
    return r.json()


def run_fetch(course: dict, date: dt.date):
    """Production fetch() for one course/date. Returns (times, error)."""
    ad_cls = ADAPTERS.get(course.get("platform"))
    if ad_cls is None:
        return None, f"no adapter for platform {course.get('platform')!r}"
    try:
        return ad_cls().fetch(course, date), None
    except Exception as exc:  # noqa: BLE001 — a failure IS the measurement
        return None, f"{type(exc).__name__}: {str(exc)[:150]}"


def section_a(reg: dict, db: D1Rest) -> None:
    print("\n" + "=" * 72)
    print("A. SUB-COURSE LABELS: adapter -> D1 -> read API")
    print("=" * 72)
    print("The widget renders displayName(name, label). A label that the")
    print("adapter produces but D1 or the API drops is invisible to a golfer,")
    print("so all three layers have to agree before this is closed.\n")

    for slug in SUBCOURSE_VENUES:
        course = reg.get(slug)
        print("-" * 72)
        if not course:
            print(f"{slug}: NOT IN REGISTRY")
            continue
        ids = course.get("ids") or {}
        print(f"{slug}  [{course.get('platform')}]  ids={ids}")

        # 1. the adapter, live
        for date in DATES:
            times, err = run_fetch(course, date)
            if err:
                print(f"   adapter {date}: {err}")
                continue
            labels = Counter(t.course_label or "(empty)" for t in times)
            print(f"   adapter {date}: {len(times)} slots | labels {dict(labels)}")
            sys.stdout.flush()

        # 2. D1, including inactive rows so the answer does not depend on
        #    there being upcoming inventory right now
        try:
            rows = db.execute(
                """SELECT course_label, active, COUNT(*) n FROM tee_times
                     WHERE course_slug = ? GROUP BY course_label, active
                     ORDER BY active DESC, n DESC""", [slug])
            if not rows:
                print("   D1: no rows at all for this slug")
            for r in rows:
                lab = r["course_label"] if r["course_label"] else "(empty)"
                state = "active" if r["active"] else "inactive"
                print(f"   D1: {lab!r} {state} x{r['n']}")
        except Exception as exc:  # noqa: BLE001
            print(f"   D1: {type(exc).__name__}: {str(exc)[:120]}")

        # 3. the read API — what the widget would actually show
        try:
            tt = api_get(f"/api/tee-times?course={slug}&limit=500")["tee_times"]
            names = Counter(t.get("course_name") or t.get("name") or "?"
                            for t in tt)
            labs = Counter(t.get("course_label") or "(empty)" for t in tt)
            print(f"   API: {len(tt)} upcoming rows | labels {dict(labs)}")
            print(f"   API display names: {dict(names)}")
        except Exception as exc:  # noqa: BLE001
            print(f"   API: {type(exc).__name__}: {str(exc)[:120]}")
        sys.stdout.flush()


def section_b(reg: dict, db: D1Rest) -> None:
    print("\n" + "=" * 72)
    print("B. ROWS WITH NO STATE — the blocker for retiring stateOf()")
    print("=" * 72)

    try:
        tot = db.execute("SELECT COUNT(*) n FROM tee_times WHERE active=1")
        miss = db.execute(
            """SELECT course_slug, city, COUNT(*) n FROM tee_times
                 WHERE active=1 AND (state IS NULL OR state='')
                 GROUP BY course_slug, city ORDER BY n DESC""")
    except Exception as exc:  # noqa: BLE001
        print(f"   D1: {type(exc).__name__}: {str(exc)[:150]}")
        return

    print(f"   {tot[0]['n']} active rows; "
          f"{sum(r['n'] for r in miss)} of them have no state, "
          f"across {len(miss)} course/city pairs\n")
    if not miss:
        print("   Nothing to backfill — the field is complete.")
    for r in miss:
        c = reg.get(r["course_slug"])
        if c is None:
            verdict = "NOT IN REGISTRY — slug drift, needs investigating"
        elif c.get("state"):
            verdict = (f"registry says state={c['state']!r} — "
                       f"the gap is in the WRITE path, backfillable")
        else:
            verdict = "registry has no state either — fix the registry first"
        print(f"   {r['course_slug']:50s} city={r['city']!r} x{r['n']}")
        print(f"      platform={(c or {}).get('platform')!r}  {verdict}")

    # The retired-slug sweep, checked directly rather than inferred from the
    # state gap closing. These two questions are not the same question: a row
    # can have a state and still belong to a course the registry has dropped,
    # and that row is a phantom course on the site either way.
    try:
        act = db.execute("SELECT course_slug, COUNT(*) n FROM tee_times "
                         "WHERE active=1 GROUP BY course_slug")
        orph = {r["course_slug"]: r["n"] for r in act
                if r["course_slug"] not in reg}
        print(f"\n   retired-slug sweep: {len(reg)} courses in the registry, "
              f"{len(act)} with active rows, "
              f"{sum(orph.values())} active rows across "
              f"{len(orph)} slug(s) the registry does not contain")
        for slug, n in sorted(orph.items(), key=lambda kv: -kv[1]):
            print(f"      STILL SERVED, NOT IN REGISTRY: {slug} x{n}")
        if not orph:
            print("      none — every active row belongs to a live course")
    except Exception as exc:  # noqa: BLE001
        print(f"   D1: {type(exc).__name__}: {str(exc)[:150]}")

    # The same question asked of the API, since that is what the widget sees.
    try:
        tt = api_get("/api/tee-times?limit=2000")["tee_times"]
        nostate = [t for t in tt if not t.get("state")]
        print(f"\n   read API: {len(tt)} rows sampled, "
              f"{len(nostate)} with no state")
        for t in nostate[:15]:
            print(f"      {t['course_slug']}  city={t.get('city')!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"   API: {type(exc).__name__}: {str(exc)[:120]}")


def section_c(reg: dict) -> None:
    print("\n" + "=" * 72)
    print("C. THE 14 CO CAPTURE MISSES, re-measured against today's registry")
    print("=" * 72)

    closed, still_open = [], []
    for slug in MISSES:
        course = reg.get(slug)
        print("-" * 72)
        if not course:
            print(f"{slug}: NOT IN REGISTRY")
            still_open.append((slug, "not in registry"))
            continue
        print(f"{slug}  [{course.get('platform')}]  "
              f"status={course.get('status')}  ids={course.get('ids')}")
        best, last_err = 0, None
        for date in DATES:
            times, err = run_fetch(course, date)
            if err:
                print(f"   {date}: {err}")
                last_err = err
            else:
                print(f"   {date}: {len(times)} slots"
                      + ("" if times else "  (EMPTY)"))
                best = max(best, len(times))
            sys.stdout.flush()
        if best > 0:
            closed.append((slug, best))
        else:
            still_open.append((slug, last_err or "returns zero on every date"))

    print("\n" + "-" * 72)
    print(f"NOW CAPTURING ({len(closed)}):")
    for slug, n in closed:
        print(f"   {slug}: up to {n} slots")
    print(f"\nSTILL OPEN ({len(still_open)}):")
    for slug, why in still_open:
        print(f"   {slug}: {why}")


def main() -> None:
    print("co_frontend_probe: Colorado + frontend follow-ups")
    print(f"dates probed: {[d.isoformat() for d in DATES]}")
    print(f"API: {API}")
    print("Report only. Nothing here edits the CSV, the registry, or D1.")
    reg = registry()
    db = D1Rest()
    section_a(reg, db)
    section_b(reg, db)
    section_c(reg)
    print("\ndone")


if __name__ == "__main__":
    main()
