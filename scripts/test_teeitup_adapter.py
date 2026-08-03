"""Offline checks for TeeItUpAdapter. No network.

THE FIXTURES ARE THE POINT OF THIS FILE. An earlier version of it passed 20
checks while the adapter was returning ZERO slots for every pinned course in
production, because its fake slots carried a top-level integer `facilityId`
that kenna has never sent. The client-side sibling filter matched that
invented key, so the bug could not reproduce here. Measured live
(probe-results/diag_kenna_slots.txt), a slot looks like:

    {"courseId": "54f14b510c8ad60378b00df6",   # Mongo id
     "teetime": "...Z", "backNine": false, "maxPlayers": 4,
     "rates": [{"greenFeeWalking": 6500, "holes": 18,
                "golfnow": {"GolfFacilityId": 287, ...}}]}

There is no facilityId key: the probe counted it None on 304/304, 51/51 and
51/51 slots across three aliases. The pinned integer (287) lives in the
FACILITIES list next to the Mongo id, and nested in each rate's GolfNow
block. So the adapter maps pin -> courseId before comparing, and every
fixture below uses the measured shape. If a future fixture invents a field,
this whole suite goes back to proving nothing.

The probe also showed kenna honours `facilityIds` server-side (287 narrowed
304 slots to 55, one courseId). The fake deliberately does NOT: it serves the
full sheet for every parameter shape, so the client filter is always
exercised. A separate check covers the already-narrowed response.

Run: python scripts/test_teeitup_adapter.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, ".")

# The adapter has a THIRD cache layer — a 7-day on-disk snapshot
# (.cache/kenna_facilities.json) shared across processes. Left enabled, the
# first Fake's successful discovery is written to disk under the REAL alias
# and every later "discovery is down" scenario silently reads it back, so
# those checks stop testing anything (and the repo grows an untracked cache
# file). Disable it before the module reads the env var; the per-instance
# _META/_FACILITIES resets below only cover the in-memory layers.
os.environ["KENNA_FACILITIES_CACHE"] = ""

from scraper.adapters.teeitup import TeeItUpAdapter, _disk_reset  # noqa: E402

_disk_reset()  # in case an earlier import already loaded a disk snapshot

DATE = dt.date(2026, 7, 26)

# Mongo courseIds, copied from the live probe.
AGUILA_CID = "54f14b510c8ad60378b00df6"     # facility 287
AGUILA9_CID = "54f14cd40c8ad60378b02e7c"    # facility 4322
CAVE_CID = "54f14b520c8ad60378b00df8"       # facility 288


def slot(utc: str, course_id: str, gn_facility: int | None, cents: int = 6500):
    """A slot in the shape kenna actually sends: no top-level facilityId."""
    rate = {"greenFeeWalking": cents, "greenFeeCart": cents + 2000,
            "holes": 18, "name": "18 Holes", "_id": 183280701}
    if gn_facility is not None:
        rate["golfnow"] = {"TTTeeTimeId": 183280701, "GolfCourseId": 163974,
                           "GolfFacilityId": gn_facility}
    return {"teetime": utc, "courseId": course_id, "backNine": False,
            "maxPlayers": 4, "minPlayers": 1, "bookedPlayers": 0,
            "rates": [rate]}


# One alias, several Phoenix munis — the shared-alias case the filter exists
# for. Served for every parameter shape so the client filter is exercised.
ALL = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": [
    slot("2026-07-26T14:30:00.000Z", AGUILA_CID, 287),
    slot("2026-07-26T15:00:00.000Z", AGUILA_CID, 287),
    slot("2026-07-26T16:10:00.000Z", AGUILA9_CID, 4322),
    slot("2026-07-26T17:20:00.000Z", CAVE_CID, 288, 7500),
]}]

# What kenna really returns for facilityIds=287: already narrowed.
NARROWED = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": [
    slot("2026-07-26T14:30:00.000Z", AGUILA_CID, 287),
    slot("2026-07-26T15:00:00.000Z", AGUILA_CID, 287),
]}]

# The same two Aguila slots with no GolfNow block — the only id left is the
# Mongo one, so this only survives if the pin was mapped through /facilities.
NO_GN = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": [
    slot("2026-07-26T14:30:00.000Z", AGUILA_CID, None),
    slot("2026-07-26T15:00:00.000Z", AGUILA_CID, None),
]}]

EMPTY = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": []}]

BOOM = RuntimeError("500 Server Error for url: .../v2/tee-times?facilityIds=287")
BARE_BOOM = RuntimeError("429 Client Error for url: .../v2/tee-times")

FACILITIES = [
    {"id": 287, "courseId": AGUILA_CID, "name": "Aguila Golf Course",
     "timeZone": "America/Phoenix"},
    {"id": 4322, "courseId": AGUILA9_CID, "name": "Aguila Golf Course 9",
     "timeZone": "America/Phoenix"},
    {"id": 288, "courseId": CAVE_CID, "name": "Cave Creek Golf Course",
     "timeZone": "America/Phoenix"},
]


class Fake(TeeItUpAdapter):
    """Replays recorded responses; records every call's params."""

    def __init__(self, with_ids=ALL, bare=EMPTY, facilities=None):
        super().__init__()
        self.with_ids = with_ids
        self.bare = bare
        self.facilities = facilities if facilities is not None else FACILITIES
        self.calls: list[dict] = []
        # per-instance caches so tests don't leak into each other
        self._META = {}
        self._FACILITIES = {}

    def get_json(self, url, **kw):  # noqa: D102
        params = kw.get("params") or {}
        self.calls.append({"url": url, "params": dict(params)})
        if "/tee-times" in url:
            v = self.with_ids if params.get("facilityIds") else self.bare
            if isinstance(v, Exception):
                raise v
            return v
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
    print("THE REGRESSION THIS FILE MISSED: a pinned course must return slots")
    a = Fake()
    out = a.fetch(AGUILA, DATE)
    check("a pinned facility_id does not empty the sheet", len(out) > 0,
          f"got {len(out)}")
    check("the pinned integer is mapped to its Mongo courseId",
          a._pinned_course_ids("city-of-phoenix-golf-courses", "287")
          == {AGUILA_CID}, str(a._pinned_course_ids(
              "city-of-phoenix-golf-courses", "287")))
    check("no slot carries a top-level facilityId to match on",
          all("facilityId" not in s for s in ALL[0]["teetimes"]))

    print("\na pinned facility_id is sent first — that is the call that works")
    sent = teetime_params(a)
    check("first tee-times call carries the pinned facilityIds",
          bool(sent) and sent[0].get("facilityIds") == "287", str(sent))
    check("exactly one tee-times call when it succeeds",
          len(sent) == 1, str(len(sent)))
    check("no bare fallback once ids answered",
          all(p.get("facilityIds") for p in sent), str(sent))

    print("\nsibling courses on a shared alias are still filtered out")
    check("only this facility's slots are kept", len(out) == 2, f"got {len(out)}")
    check("no sibling slot leaked",
          all(t.raw["courseId"] == AGUILA_CID for t in out),
          str({t.raw["courseId"] for t in out}))
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
    a = Fake()
    out = a.fetch(course("aguila-9-golf-course",
                         {"alias": "city-of-phoenix-golf-courses",
                          "facility_id": "4322"}), DATE)
    check("aguila-9 gets exactly its own slot",
          len(out) == 1 and out[0].raw["courseId"] == AGUILA9_CID,
          f"got {len(out)}")

    print("\nkenna's own server-side filtering passes through untouched")
    a = Fake(with_ids=NARROWED)
    out = a.fetch(AGUILA, DATE)
    check("an already-narrowed response is not re-filtered to zero",
          len(out) == 2, f"got {len(out)}")

    print("\nthe Mongo id alone is enough — no GolfNow block needed")
    a = Fake(with_ids=NO_GN)
    out = a.fetch(AGUILA, DATE)
    check("slots without a golfnow block still match via courseId",
          len(out) == 2, f"got {len(out)}")

    print("\nthe nested GolfFacilityId carries the pin when /facilities is down")
    a = Fake(facilities=RuntimeError("429"))
    out = a.fetch(AGUILA, DATE)
    check("pin still resolves through rates[].golfnow.GolfFacilityId",
          len(out) == 2, f"got {len(out)}")
    check("and still excludes the siblings",
          all(t.raw["courseId"] == AGUILA_CID for t in out),
          str({t.raw["courseId"] for t in out}))

    print("\nunresolvable pin: one course is unambiguous, several is not")
    a = Fake(with_ids=NO_GN, facilities=RuntimeError("429"))
    out = a.fetch(AGUILA, DATE)
    check("a single-course response is kept rather than dropped",
          len(out) == 2, f"got {len(out)}")
    a = Fake(with_ids=[{"dayInfo": {}, "teetimes": [
        slot("2026-07-26T14:30:00.000Z", AGUILA_CID, None),
        slot("2026-07-26T16:10:00.000Z", AGUILA9_CID, None)]}],
        facilities=RuntimeError("429"))
    out = a.fetch(AGUILA, DATE)
    check("a multi-course response is NOT published under one name",
          out == [], f"got {len(out)}")

    print("\nan unpinned alias discovers its ids instead of asking bare")
    a = Fake()
    out = a.fetch(course("city-of-phoenix",
                         {"alias": "city-of-phoenix-golf-courses"}), DATE)
    check("discovered ids are sent",
          teetime_params(a)[0].get("facilityIds") == "287,4322,288",
          str(teetime_params(a)))
    check("every slot is returned", len(out) == 4, f"got {len(out)}")
    check("labels come from facility metadata",
          {t.course_label for t in out} == {"Aguila Golf Course",
                                            "Aguila Golf Course 9",
                                            "Cave Creek Golf Course"},
          str({t.course_label for t in out}))

    print("\nan empty day is an empty day, not a retry and not a raise")
    a = Fake(with_ids=EMPTY)
    out = a.fetch(AGUILA, DATE)
    check("returns []", out == [], str(out))
    check("no second tee-times call", len(teetime_params(a)) == 1,
          str(teetime_params(a)))

    print("\na failed ids call falls back — pinned, then discovered, then bare")
    a = Fake(with_ids=BOOM, bare=ALL)
    out = a.fetch(AGUILA, DATE)
    sent = teetime_params(a)
    check("pinned tried first, bare tried last",
          [p.get("facilityIds") for p in sent] == ["287", "287,4322,288", None],
          str(sent))
    check("the bare fallback's slots are still filtered", len(out) == 2,
          f"got {len(out)}")

    print("\ndiscovery failure does not block the bare fallback")
    a = Fake(with_ids=BOOM, bare=ALL, facilities=RuntimeError("404"))
    out = a.fetch(AGUILA, DATE)
    check("bare call still made",
          [p.get("facilityIds") for p in teetime_params(a)] == ["287", None],
          str(teetime_params(a)))
    check("slots returned", len(out) == 2, f"got {len(out)}")

    print("\nwhen every shape fails, the FIRST error is what surfaces")
    a = Fake(with_ids=BOOM, bare=BARE_BOOM)
    try:
        a.fetch(AGUILA, DATE)
        check("raises when kenna is down", False, "returned instead")
    except RuntimeError as exc:
        check("raises when kenna is down", "500" in str(exc), str(exc)[:60])

    print("\ndiscovery asks the route that is actually alive, first")
    a = Fake()
    a.discover_facilities("city-of-phoenix-golf-courses")
    meta = [c["url"] for c in a.calls if "/tee-times" not in c["url"]]
    check("/alias/<alias>/facilities is tried before /v2/courses",
          bool(meta) and meta[0].endswith(
              "/alias/city-of-phoenix-golf-courses/facilities"), str(meta))
    check("and nothing else is asked once it answers", len(meta) == 1, str(meta))
    a = Fake(facilities=RuntimeError("404"))
    try:
        a.discover_facilities("city-of-phoenix-golf-courses")
    except Exception:  # noqa: BLE001 — both routes down is the point here
        pass
    meta = [c["url"] for c in a.calls if "/tee-times" not in c["url"]]
    check("/v2/courses is still tried when the alias route fails",
          any(u.endswith("/v2/courses") for u in meta), str(meta))

    print("\nfacility metadata failure degrades, it does not raise")
    a = Fake(facilities=RuntimeError("404"))
    out = a.fetch(AGUILA, DATE)
    check("slots still returned without metadata", len(out) == 2, f"got {len(out)}")
    check("falls back to the state timezone",
          out[0].teetime == "2026-07-26T07:30:00", out[0].teetime)

    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(FAILS) + ' CHECK(S) FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
