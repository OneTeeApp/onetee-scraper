"""Offline checks for TeeItUpAdapter. No network.

Locks in the CORRECTED behaviour after the facilityIds round trip.

a248c79 stopped sending `facilityIds` at all, on the strength of one diag4
sample in which every alias answered HTTP 500 to the param. #69 measured that
claim against live sheets and it was wrong (probe-results/verify_fixes.txt
section A): the pinned call returns the real sheet on every alias that has
one, and the bare per-alias call returns an EMPTY teetimes list — 869 slots
before, 0 after. kenna's gateway just 5xxs and 429s intermittently, which is
why get_json retries 5xx in the first place.

So the adapter asks with ids first (pinned, else discovered), falls back to
the bare call only when a call actually FAILS, and keeps filtering
client-side so a shared alias can never publish a sibling's tee sheet. An
empty response is an empty day and must not trigger a retry or a raise.

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


# One alias, four Phoenix munis. Served for any facilityIds call, so the
# client-side filter is exercised even when kenna honours the param.
ALL = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": [
    slot("2026-07-26T14:30:00.000Z", 287, 287),        # aguila
    slot("2026-07-26T15:00:00.000Z", 287, 287),
    slot("2026-07-26T16:10:00.000Z", 4322, 4322),      # aguila-9
    slot("2026-07-26T17:20:00.000Z", 288, 288, 7500),  # cave creek
]}]

EMPTY = [{"dayInfo": {"date": "2026-07-26"}, "teetimes": []}]

BOOM = RuntimeError("500 Server Error for url: .../v2/tee-times?facilityIds=287")
BARE_BOOM = RuntimeError("429 Client Error for url: .../v2/tee-times")

FACILITIES = [
    {"id": 287, "courseId": 287, "name": "Aguila Golf Course",
     "timeZone": "America/Phoenix"},
    {"id": 4322, "courseId": 4322, "name": "Aguila 9",
     "timeZone": "America/Phoenix"},
    {"id": 288, "courseId": 288, "name": "Cave Creek Golf Course",
     "timeZone": "America/Phoenix"},
]


class Fake(TeeItUpAdapter):
    """Replays recorded responses; records every call's params.

    Defaults mirror what kenna actually does: the call with ids answers, the
    bare call comes back empty.
    """

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
    print("a pinned facility_id is sent first — that is the call that works")
    a = Fake()
    out = a.fetch(AGUILA, DATE)
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
    a = Fake()
    out = a.fetch(course("aguila-9-golf-course",
                         {"alias": "city-of-phoenix-golf-courses",
                          "facility_id": "4322"}), DATE)
    check("aguila-9 gets exactly its own slot",
          len(out) == 1 and str(out[0].raw["facilityId"]) == "4322",
          f"got {len(out)}")

    print("\nan unpinned alias discovers its ids instead of asking bare")
    a = Fake()
    out = a.fetch(course("city-of-phoenix",
                         {"alias": "city-of-phoenix-golf-courses"}), DATE)
    check("discovered ids are sent",
          teetime_params(a)[0].get("facilityIds") == "287,4322,288",
          str(teetime_params(a)))
    check("every slot is returned", len(out) == 4, f"got {len(out)}")
    check("labels come from facility metadata",
          {t.course_label for t in out} == {"Aguila Golf Course", "Aguila 9",
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
