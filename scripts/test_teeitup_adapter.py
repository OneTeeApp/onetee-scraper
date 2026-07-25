"""Offline checks for TeeItUpAdapter. No network.

Locks in the fix for the facilityIds regression (probe-results/diag4.txt
section E): kenna now answers HTTP 500 to any `facilityIds` param, on every
alias including controls we publish from daily. The adapter used to

  * pass a pinned facility_id straight into the FIRST call, so all ten
    pinned courses (four of which share the city-of-phoenix alias) failed
    on every scrape, and
  * retry with facilityIds, uncaught, whenever the bare call came back
    empty — so every legitimately-empty day raised too.

Now it always asks for the whole alias and filters client-side.

Run: python scripts/test_teeitup_adapter.py
"""
from __future__ import annotations

import datetime as dt
import sys

sys.path.insert(0, ".")

from scraper.adapters.teeitup import TeeItUpAdapter  # noqa: E402

DATE = dt.date(2026, 7, 26)


def slot(utc: str, facility: int, course_id: int, cents: int = 6500) -> dict:
    return {"teetime": utc, "facilityId": facility, "courseId": course_id,
            "maxPlayers": 4, "minPlayers": 1,
            "rates": [{"greenFeeWalking": cents, "greenFeeCart": cents + 2000,
                       "holes": 18, "name": "Standard"}]}


# One alias, four Phoenix munis. A bare call returns all of them.
ALL = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": [
    slot("2026-07-26T14:30:00.000Z", 287, 287),        # aguila
    slot("2026-07-26T15:00:00.000Z", 287, 287),
    slot("2026-07-26T16:10:00.000Z", 4322, 4322),      # aguila-9
    slot("2026-07-26T17:20:00.000Z", 288, 288, 7500),  # cave creek
]}]

EMPTY = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": []}]

BOOM = RuntimeError("500 Server Error for url: .../v2/tee-times?facilityIds=287")


class Fake(TeeItUpAdapter):
    """Replays recorded responses; records every call's params."""

    def __init__(self, bare, with_ids=BOOM, facilities=None):
        super().__init__()
        self.bare = bare
        self.with_ids = with_ids
        self.facilities = facilities if facilities is not None else [
            {"id": 287, "courseId": 287, "name": "Aguila Golf Course",
             "timeZone": "America/Phoenix"},
            {"id": 4322, "courseId": 4322, "name": "Aguila 9",
             "timeZone": "America/Phoenix"},
            {"id": 288, "courseId": 288, "name": "Cave Creek Golf Course",
             "timeZone": "America/Phoenix"},
        ]
        self.calls: list[dict] = []
        # per-instance meta cache so tests don't leak into each other
        self._META = {}

    def get_json(self, url, **kw):  # noqa: D102
        params = kw.get("params") or {}
        self.calls.append({"url": url, "params": dict(params)})
        if "/tee-times" in url:
            v = self.with_ids if params.get("facilityIds") else self.bare
            if isinstance(v, Exception):
                raise v
            return v
        # facility metadata
        v = self.facilities
        if isinstance(v, Exception):
            raise v
        return v


def course(slug: str, ids: dict) -> dict:
    return {"slug": slug, "name": slug.replace("-", " ").title(),
            "platform": "teeitup", "state": "AZ", "city": "Phoenix",
            "venue_id": slug, "source_role": "primary",
            "booking_url": "https://x.book.teeitup.com/", "ids": ids}


AGUILA = course("aguila-golf-course",
                {"alias": "city-of-phoenix-golf-courses", "facility_id": "287"})
FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS += 1


def teetime_params(a: Fake) -> list[dict]:
    return [c["params"] for c in a.calls if "/tee-times" in c["url"]]


def main() -> None:
    print("a pinned facility_id must never be sent to kenna")
    a = Fake(ALL)
    out = a.fetch(AGUILA, DATE)
    sent = teetime_params(a)
    check("first tee-times call carries no facilityIds",
          sent and "facilityIds" not in sent[0], str(sent))
    check("facilityIds is never sent at all",
          all("facilityIds" not in p for p in sent), str(sent))
    check("exactly one tee-times call", len(sent) == 1, str(len(sent)))

    print("\nsibling courses on a shared alias are filtered out")
    check("only this facility's slots are kept", len(out) == 2, f"got {len(out)}")
    check("no sibling slot leaked",
          all(str(t.raw["facilityId"]) == "287" for t in out))
    check("one course after filtering -> unlabelled",
          all(t.course_label == "" for t in out),
          str({t.course_label for t in out}))
    check("UTC converted to course-local Phoenix time",
          [t.teetime for t in out] == ["2026-07-26T07:30:00",
                                       "2026-07-26T08:00:00"],
          str([t.teetime for t in out]))
    check("cents -> dollars", (out[0].price_min, out[0].price_max) == (65.0, 85.0),
          f"{out[0].price_min}/{out[0].price_max}")

    print("\nthe 9-hole sibling resolves to its own facility")
    a = Fake(ALL)
    out = a.fetch(course("aguila-9-golf-course",
                         {"alias": "city-of-phoenix-golf-courses",
                          "facility_id": "4322"}), DATE)
    check("aguila-9 gets exactly its own slot",
          len(out) == 1 and str(out[0].raw["facilityId"]) == "4322",
          f"got {len(out)}")

    print("\nan empty day is an empty day, not a facilityIds retry")
    a = Fake(EMPTY)
    out = a.fetch(course("granby-ranch", {"alias": "granby-ranch"}), DATE)
    check("returns []", out == [], str(out))
    check("no facilityIds retry was attempted",
          all("facilityIds" not in p for p in teetime_params(a)),
          str(teetime_params(a)))

    print("\nunpinned multi-course alias still gets sub-course labels")
    a = Fake(ALL)
    out = a.fetch(course("city-of-phoenix", {"alias": "city-of-phoenix-golf-courses"}),
                  DATE)
    check("every slot is returned", len(out) == 4, f"got {len(out)}")
    check("labels come from facility metadata",
          {t.course_label for t in out} == {"Aguila Golf Course", "Aguila 9",
                                            "Cave Creek Golf Course"},
          str({t.course_label for t in out}))

    print("\na bare-call failure still raises the bare-call error")
    a = Fake(BOOM, with_ids=BOOM)
    try:
        a.fetch(AGUILA, DATE)
        check("raises when kenna is down", False, "returned instead")
    except RuntimeError as exc:
        check("raises when kenna is down", "500" in str(exc), str(exc)[:60])

    print("\nfacility metadata failure degrades, it does not raise")
    a = Fake(ALL, facilities=RuntimeError("404"))
    out = a.fetch(AGUILA, DATE)
    check("slots still returned without metadata", len(out) == 2, f"got {len(out)}")
    check("falls back to the state timezone",
          out[0].teetime == "2026-07-26T07:30:00", out[0].teetime)

    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(FAILS) + ' CHECK(S) FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
