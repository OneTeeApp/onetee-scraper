"""Offline checks for TeesnapAdapter. No network.

Locks in the fix for the Hollydot / Petteys Park capture miss
(probe-results/diag4.txt section B):

  * window.courses embeds each course's parent PROPERTY object, which has its
    own "id"/"created_at" pair. The old regex harvested those as course ids.
  * Hollydot's property id 1329 and Petteys Park's 1081 answer HTTP 500
    "Be right back." on the tee-sheet route, and that exception escaped
    fetch(), throwing away the 60-70 real slots the course's own id returned.

Run: python scripts/test_teesnap_adapter.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")

from scraper.adapters.teesnap import TeesnapAdapter  # noqa: E402

DATE = dt.date(2026, 7, 26)

# Homepage shape: two live courses, one deleted, each embedding its property.
# The property ids (1329) must NOT be treated as courses.
HOMEPAGE = "<html><script>window.courses = " + json.dumps([
    {"id": 1550, "created_at": "2023-05-18T16:09:13.000000Z",
     "deleted_at": None, "property_id": 1329, "key": "hollydotgolf",
     "name": "Hollydot Golf Course", "enabled": True,
     "property": {"id": 1329, "created_at": "2020-01-01T00:00:00.000000Z",
                  "name": "Hollydot Golf Course, CO"},
     "tee_sheets": [{"id": 1767, "created_at": "2021-01-01T00:00:00.000000Z"}]},
    {"id": 1551, "created_at": "2023-05-18T16:09:13.000000Z",
     "deleted_at": None, "property_id": 1329, "key": "hollydot-nine",
     "name": "Hollydot Nine", "enabled": True,
     "property": {"id": 1329, "created_at": "2020-01-01T00:00:00.000000Z"}},
    {"id": 1552, "created_at": "2019-01-01T00:00:00.000000Z",
     "deleted_at": "2024-06-01T00:00:00.000000Z", "key": "old-course",
     "name": "Retired Course", "enabled": True},
]) + ";</script></html>"


def slot(hhmm: str, price18: str = "38.00") -> dict:
    return {
        "prices": [{"roundType": "NINE_HOLE", "price": "26.00"},
                   {"roundType": "EIGHTEEN_HOLE", "price": price18}],
        "teeOffSections": [{"teeOff": "FRONT_NINE",
                            "turnTo": {"time": f"2026-07-26T{hhmm}:00"}}],
    }


def sheet(times: list[str], price18: str = "38.00") -> dict:
    return {"teeTimes": {"teeTimes": [slot(t, price18) for t in times],
                         "bookings": []}}


class Fake(TeesnapAdapter):
    """Replays recorded responses; records what was requested."""

    def __init__(self, sheets: dict[int, Any], homepage: str = HOMEPAGE):
        super().__init__()
        self.sheets = sheets
        self.homepage = homepage
        self.asked: list[int] = []

    def _get_text(self, url):  # noqa: D102
        return self.homepage

    def get_json(self, url, **kw):  # noqa: D102
        cid = int(kw["params"]["course"])
        self.asked.append(cid)
        v = self.sheets.get(cid)
        if isinstance(v, Exception):
            raise v
        return v if v is not None else {"teeTimes": {"teeTimes": []}}


COURSE = {"slug": "hollydot-golf-course", "name": "Hollydot Golf Course",
          "platform": "teesnap", "state": "CO", "city": "Colorado City",
          "venue_id": "hollydot-golf-course", "source_role": "primary",
          "booking_url": "https://hollydotgolf.teesnap.net/",
          "ids": {"subdomain": "hollydotgolf"}}

FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS += 1


def main() -> None:
    print("discover_courses")
    a = Fake({})
    found = a.discover_courses("hollydotgolf")
    ids = [c["id"] for c in found]
    check("only top-level courses are returned", ids == [1550, 1551], f"got {ids}")
    check("the property id is not mistaken for a course", 1329 not in ids)
    check("a nested tee_sheets id is not mistaken for a course", 1767 not in ids)
    check("deleted courses are skipped", 1552 not in ids)
    check("names come back with the ids",
          [c["name"] for c in found] == ["Hollydot Golf Course", "Hollydot Nine"])

    print("\nfetch: a 500 on one id must not cost the venue its real slots")
    boom = RuntimeError("500 Server Error: Be right back.")
    a = Fake({1550: sheet(["07:10", "07:20", "07:30"]), 1551: boom})
    out = a.fetch(COURSE, DATE)
    check("the good id's slots survive", len(out) == 3, f"got {len(out)}")
    check("no property id was ever requested", 1329 not in a.asked,
          f"asked {a.asked}")

    print("\nfetch: total failure is still an error, not an empty day")
    a = Fake({1550: boom, 1551: boom})
    try:
        a.fetch(COURSE, DATE)
        check("raises when every id fails", False, "returned instead")
    except RuntimeError as exc:
        check("raises when every id fails", "every Teesnap course id" in str(exc))

    print("\nfetch: a genuinely empty sheet is an empty day, not an error")
    a = Fake({1550: {"teeTimes": {"teeTimes": []}},
              1551: {"teeTimes": {"teeTimes": []}}})
    check("empty sheets return []", a.fetch(COURSE, DATE) == [])

    print("\nfetch: labels and prices")
    a = Fake({1550: sheet(["07:10"]), 1551: sheet(["08:00"], price18="44.00")})
    out = sorted(a.fetch(COURSE, DATE), key=lambda t: t.teetime)
    check("two live courses -> both labelled",
          {t.course_label for t in out} == {"Hollydot Golf Course", "Hollydot Nine"},
          str({t.course_label for t in out}))
    check("holes parsed from roundType", out[0].holes == [9, 18], str(out[0].holes))
    check("price range per slot",
          (out[0].price_min, out[0].price_max) == (26.0, 38.0)
          and out[1].price_max == 44.0)

    print("\nfetch: single live course stays unlabelled")
    single = ("<script>window.courses = " + json.dumps([
        {"id": 1550, "created_at": "2023-05-18T16:09:13.000000Z",
         "deleted_at": None, "key": "hollydotgolf",
         "name": "Hollydot Golf Course",
         "property": {"id": 1329, "created_at": "2020-01-01T00:00:00.000000Z"}},
    ]) + ";</script>")
    a = Fake({1550: sheet(["07:10"])}, homepage=single)
    out = a.fetch(COURSE, DATE)
    check("one course -> no course_label",
          all(t.course_label == "" for t in out) and len(out) == 1)

    print("\nfetch: pinned registry ids bypass discovery")
    c = dict(COURSE, ids={"subdomain": "hollydotgolf",
                          "teesnap_course_ids": [1550]})
    a = Fake({1550: sheet(["07:10", "07:20"])})
    out = a.fetch(c, DATE)
    check("only the pinned id is requested", a.asked == [1550], str(a.asked))
    check("pinned single id -> unlabelled",
          len(out) == 2 and all(t.course_label == "" for t in out))

    print("\nfetch: unparseable window.courses falls back to the old anchor")
    a = Fake({1550: sheet(["07:10"])},
             homepage='<script>window.courses = [{"id": 1550, "created_at": '
                      '"2023-01-01"}, BROKEN;</script>')
    ids = [c["id"] for c in a.discover_courses("hollydotgolf")]
    check("regex fallback still finds the id", ids == [1550], str(ids))

    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(FAILS) + ' CHECK(S) FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    from typing import Any  # noqa: F401 — used in the Fake signature above
    main()
