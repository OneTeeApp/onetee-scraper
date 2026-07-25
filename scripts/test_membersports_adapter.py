"""Offline test for MemberSportsAdapter's configuration sweep.

No network: the tee-sheet POST is replaced with recorded response shapes taken
from probe-results/diag2.txt, so this proves the label logic rather than the
API. Run with `python scripts/test_membersports_adapter.py`.

What it pins down:
  * Kennedy's sub-course labels come from sweeping configurationTypeId. At
    cfg 0 the club returns only the Par 3 sheet; cfg 1 and cfg 2 return the two
    18-hole configurations, both under golfCourseId 20573 but different names.
    So the sub-course key must be (golfCourseId, name), not the id alone.
  * A single-sheet club still emits course_label "" — labels appear only when
    the day genuinely spans more than one sub-course.
  * course_ids, when pinned, keeps a shared club from serving its neighbour's
    tee sheet.
  * An all-configurations failure raises instead of quietly reporting an
    empty day, which would look like a course with no inventory.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper.adapters.experimental import MemberSportsAdapter  # noqa: E402

DATE = dt.date(2026, 7, 26)


def item(cid: int, name: str, players: int = 0, price: float = 45.0,
         holes: int = 18, **kw) -> dict:
    d = {"golfCourseId": cid, "name": name, "playerCount": players,
         "price": price, "golfCourseNumberOfHoles": holes,
         "bookingNotAllowed": False, "hide": False}
    d.update(kw)
    return d


# Kennedy (club 3629) exactly as diag2 recorded it: three sheets spread across
# three configurationTypeIds, two of them sharing golfCourseId 20573.
KENNEDY = {
    0: [{"teeTime": 420, "items": [item(4669, "Kennedy Par 3 or Footgolf",
                                        holes=9, price=18.0)]}],
    1: [{"teeTime": 420, "items": [item(20573, "Kennedy (Babe Lind / Creek)")]},
        {"teeTime": 430, "items": [item(20573, "Kennedy (Babe Lind / Creek)",
                                        players=2, price=52.0)]}],
    2: [{"teeTime": 420, "items": [item(20573, "Kennedy (West 9 only)",
                                        holes=9, price=25.0)]}],
}

# Foothills (club 3697): all three sheets arrive at cfg 0.
FOOTHILLS = {
    0: [{"teeTime": 360, "items": [item(4757, "Foothills Par 3", holes=9),
                                   item(4758, "Foothills Executive 9", holes=9),
                                   item(4759, "Foothills 18 Back Nine")]}],
}

# City Park (club 3660): one course, one sheet.
CITY_PARK = {0: [{"teeTime": 480, "items": [item(4711, "City Park")]}]}


def make(ids: dict, sheets: dict, slug: str = "test-course"):
    """An adapter whose tee-sheet POST replays `sheets` keyed by cfg."""
    a = MemberSportsAdapter()
    calls: list[dict] = []

    def fake_post_json(url, *, json=None, headers=None, timeout=None):
        calls.append(json)
        if json["configurationTypeId"] not in sheets:
            raise RuntimeError("simulated gateway failure")
        return sheets[json["configurationTypeId"]]

    a.post_json = fake_post_json  # type: ignore[method-assign]
    course = {"slug": slug, "name": slug, "city": "Denver", "state": "CO",
              "platform": "membersports", "venue_id": slug,
              "source_role": "primary", "booking_url": "", "ids": ids}
    return a, course, calls


FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          + (f"   ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def test_kennedy() -> None:
    print("Kennedy — sub-course labels require sweeping configurationTypeId")
    ids = {"club_id": "3629", "secondary_id": "20573",
           "config_ids": [0, 1, 2]}
    a, course, calls = make(ids, KENNEDY, "kennedy-golf-course")
    times = a.fetch(course, DATE)
    labels = sorted({t.course_label for t in times})

    check("sweeps every declared configuration", len(calls) == 3,
          f"called {len(calls)}")
    check("always asks for golfCourseId 0",
          all(c["golfCourseId"] == 0 for c in calls))
    check("never sets golfClubGroupId 1",
          all(c["golfClubGroupId"] == 0 for c in calls))
    check("all three sheets captured", len(times) == 4, f"{len(times)} slots")
    check("labels populated (the actual bug)",
          labels == ["Kennedy (Babe Lind / Creek)", "Kennedy (West 9 only)",
                     "Kennedy Par 3 or Footgolf"], str(labels))

    # 07:00 exists on all three sheets. Under the old (teeTime, golfCourseId)
    # key the two 20573 sheets collapsed into one row and one of them was lost.
    seven = [t for t in times if t.teetime.endswith("T07:00:00")]
    check("same time on 3 sheets stays 3 rows", len(seven) == 3,
          f"{len(seven)} rows at 07:00")
    check("D1 primary key is unique",
          len({(t.course_slug, t.teetime, t.course_label) for t in times})
          == len(times))

    par3 = next(t for t in times if t.course_label.startswith("Kennedy Par 3"))
    check("per-sheet price kept separate", par3.price_min == 18.0,
          str(par3.price_min))
    check("per-sheet holes kept separate", par3.holes == [9], str(par3.holes))


def test_foothills_shared_club() -> None:
    print("\nFoothills — one club, three sheets, all at cfg 0")
    a, course, calls = make({"club_id": "3697", "secondary_id": "4758"},
                            FOOTHILLS, "foothills-golf-course")
    times = a.fetch(course, DATE)
    check("no course_ids => keep every sheet the club owns", len(times) == 3,
          f"{len(times)}")
    check("defaults to a single cfg 0 request", len(calls) == 1)
    check("labelled", sorted(t.course_label for t in times) ==
          ["Foothills 18 Back Nine", "Foothills Executive 9",
           "Foothills Par 3"])

    print("  pinning course_ids fences a venue off from its club-mate")
    a2, c2, _ = make({"club_id": "3697", "secondary_id": "4758",
                      "course_ids": [4758]}, FOOTHILLS, "meadows-golf-club")
    t2 = a2.fetch(c2, DATE)
    check("only the pinned sheet survives",
          [t.course_label for t in t2] == [""], str(t2 and t2[0].course_label))


def test_single_course_club() -> None:
    print("\nCity Park — single sheet stays unlabelled")
    a, course, _ = make({"club_id": "3660", "secondary_id": "4711"},
                        CITY_PARK, "city-park-golf-course")
    times = a.fetch(course, DATE)
    check("one slot", len(times) == 1)
    check("course_label empty for a single-course facility",
          times[0].course_label == "", repr(times[0].course_label))


def test_sold_out_and_hidden() -> None:
    print("\nFiltering — full, hidden and unbookable slots are dropped")
    sheets = {0: [{"teeTime": 420, "items": [
        item(4711, "City Park", players=4),                 # full
        item(4711, "City Park", hide=True),
        item(4711, "City Park", bookingNotAllowed=True),
    ]}]}
    a, course, _ = make({"club_id": "3660", "secondary_id": "4711"}, sheets)
    check("nothing emitted", a.fetch(course, DATE) == [])


def test_partial_and_total_failure() -> None:
    print("\nFailure handling")
    # cfg 1 present, cfg 9 missing -> partial sweep still returns what it got.
    a, course, _ = make({"club_id": "3629", "secondary_id": "20573",
                         "config_ids": [1, 9]}, KENNEDY)
    check("a partial sweep still returns its findings",
          len(a.fetch(course, DATE)) == 2)

    a2, c2, _ = make({"club_id": "3629", "secondary_id": "20573",
                      "config_ids": [8, 9]}, KENNEDY)
    try:
        a2.fetch(c2, DATE)
        check("total failure raises instead of returning empty", False,
              "returned normally")
    except RuntimeError as exc:
        check("total failure raises instead of returning empty",
              "all MemberSports configurations failed" in str(exc))


def main() -> int:
    test_kennedy()
    test_foothills_shared_club()
    test_single_course_club()
    test_sold_out_and_hidden()
    test_partial_and_total_failure()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all checks passed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
