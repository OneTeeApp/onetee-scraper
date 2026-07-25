"""Offline checks for deactivate_unknown_slugs — run against a temp SQLite DB.

WHY THIS FUNCTION EXISTS AT ALL, restated so the tests below read as
requirements rather than trivia:

sync() deactivates a row only when its course appears in the scrape being
diffed. That guard is correct and must not be removed — without it, a course
that errors, or a shard that fails, would blink its whole tee sheet off the
site. The cost is that a slug which LEAVES the registry is in no scrape ever
again, so nothing revisits its rows. Measured: 135 active rows across three
retired pre-`course_label` slugs, and those same rows were every row in D1
with no `state`, which is why the widget's city-inference fallback was filing
Arizona courses under Colorado.

The dangerous failure mode is the opposite one: a sweep keyed on the wrong
authority deactivating live inventory. So the checks are weighted towards
proving what this function must NEVER touch — sharding (a registry course
absent from the current scrape), already-inactive rows, and the case where
the registry itself fails to load.

No network. Nothing here touches production D1.
"""
from __future__ import annotations

import os
import json
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.d1 import (SqliteLocal, init_schema,  # noqa: E402
                        deactivate_unknown_slugs)

PASS, FAIL = 0, 0
STAMP = "2026-07-25T00:00:00+00:00"


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


def make_db(rows):
    """rows = [(slug, teetime, active), ...]"""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    db = SqliteLocal(path)
    init_schema(db)
    for slug, teetime, active in rows:
        db.execute(
            "INSERT OR REPLACE INTO tee_times (course_slug, teetime, "
            "course_label, course_name, active, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [slug, teetime, "", slug, active, STAMP, STAMP])
    return db, path


def make_registry(slugs):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"courses": [{"slug": s, "state": "AZ"} for s in slugs]}, fh)
    return path


def active_slugs(db) -> dict:
    return {r["course_slug"]: r["n"] for r in db.execute(
        "SELECT course_slug, COUNT(*) n FROM tee_times "
        "WHERE active=1 GROUP BY course_slug")}


def main() -> int:
    # ---------------------------------------------------------------- #
    print("\nTHE THREE REAL ORPHANS, and the live courses beside them")
    # ---------------------------------------------------------------- #
    db, _ = make_db([
        # retired per-sub-course slugs, exactly as found in production
        ("gold-canyon-golf-resort-dinosaur-mountain-sidewinder", "2026-07-26T08:00:00", 1),
        ("gold-canyon-golf-resort-dinosaur-mountain-sidewinder", "2026-07-26T08:10:00", 1),
        ("grayhawk-golf-club-raptor-talon", "2026-07-26T09:00:00", 1),
        ("mountain-view-golf-course-fort-huachuca", "2026-07-26T10:00:00", 1),
        # the real courses those slugs decompose to — must survive
        ("gold-canyon-golf-resort", "2026-07-26T08:00:00", 1),
        ("grayhawk-golf-club", "2026-07-26T09:00:00", 1),
        ("mountain-view-golf-course", "2026-07-26T10:00:00", 1),
    ])
    reg = make_registry(["gold-canyon-golf-resort", "grayhawk-golf-club",
                         "mountain-view-golf-course"])

    res = deactivate_unknown_slugs(db, reg, dry_run=True)
    check("dry run reports all four orphan rows", res["deactivated"], 4)
    check("dry run names the three retired slugs", sorted(res["slugs"]), [
        "gold-canyon-golf-resort-dinosaur-mountain-sidewinder",
        "grayhawk-golf-club-raptor-talon",
        "mountain-view-golf-course-fort-huachuca"])
    check("dry run writes NOTHING", len(active_slugs(db)), 6)

    res = deactivate_unknown_slugs(db, reg)
    check("live run deactivates the same four", res["deactivated"], 4)
    check("the three base-slug courses are untouched",
          sorted(active_slugs(db)), ["gold-canyon-golf-resort",
                                     "grayhawk-golf-club",
                                     "mountain-view-golf-course"])
    check("re-running is a no-op (idempotent)",
          deactivate_unknown_slugs(db, reg)["deactivated"], 0)

    # ---------------------------------------------------------------- #
    print("\nWHAT IT MUST NEVER TOUCH")
    # ---------------------------------------------------------------- #
    # Sharding: this is the whole reason the REGISTRY is the authority and
    # the current scrape is not. A course can legitimately be missing from
    # every scrape for hours (its shard failed, its adapter errored) and its
    # rows must stay live.
    db2, _ = make_db([("scraped-today", "2026-07-26T08:00:00", 1),
                      ("errored-this-run", "2026-07-26T08:00:00", 1),
                      ("in-a-failed-shard", "2026-07-26T08:00:00", 1)])
    reg2 = make_registry(["scraped-today", "errored-this-run",
                          "in-a-failed-shard"])
    res = deactivate_unknown_slugs(db2, reg2)
    check("a registry course absent from every scrape is NOT deactivated",
          res["deactivated"], 0)
    check("all three remain active", len(active_slugs(db2)), 3)

    # Already-inactive orphan rows are not re-counted as work done.
    db3, _ = make_db([("retired-slug", "2026-07-26T08:00:00", 0),
                      ("retired-slug", "2026-07-26T08:10:00", 1)])
    reg3 = make_registry(["something-else"])
    res = deactivate_unknown_slugs(db3, reg3)
    check("only the ACTIVE orphan row is counted", res["deactivated"], 1)
    check("nothing is left active", active_slugs(db3), {})

    # An unreadable or empty registry must fail loudly rather than treat
    # every course in D1 as retired.
    db4, _ = make_db([("a-real-course", "2026-07-26T08:00:00", 1)])
    empty = make_registry([])
    try:
        deactivate_unknown_slugs(db4, empty)
        check("an EMPTY registry raises rather than wiping D1", "no raise",
              "RuntimeError")
    except RuntimeError:
        check("an EMPTY registry raises rather than wiping D1",
              "RuntimeError", "RuntimeError")
    check("the real course is still active after that refusal",
          active_slugs(db4), {"a-real-course": 1})

    # ---------------------------------------------------------------- #
    print("\nSHAPE")
    # ---------------------------------------------------------------- #
    db5, _ = make_db([("ghost", "2026-07-26T08:00:00", 1)])
    reg5 = make_registry(["real-one", "real-two"])
    res = deactivate_unknown_slugs(db5, reg5)
    check("known_courses counts the registry, not D1",
          res["known_courses"], 2)
    check("slugs maps slug -> row count", res["slugs"], {"ghost": 1})

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
