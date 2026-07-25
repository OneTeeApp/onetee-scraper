"""What is actually IN a kenna tee-time slot, and how does it name its course?

verify_fixes.txt (run at 2d6b1d6) says TeeItUp fetch() returns ZERO for all
ten pinned courses while the raw call on the same alias and date returns
hundreds of slots. Both of my last two readings of that section blamed the
`facilityIds` PARAMETER. Both were wrong in the same way: the BEFORE column
counts RAW slots off _teetimes(), the AFTER column runs the whole of fetch(),
and the one thing fetch() does that the raw call does not is the client-side
sibling filter a248c79 added:

    want = {"287"}
    if want and not ({str(slot["facilityId"]), str(slot["courseId"])} & want):
        continue

diag_golden_hills.txt shows a slot carrying `"courseId":
"54f14bc50c8ad60378b0163a"` — a Mongo id — while the pinned facility_id is
the integer 1295 out of /alias/<alias>/facilities. If slots never carry the
integer, that set intersection is empty for every slot on every course, and
the filter silently deletes the entire sheet. That fits the measurement
exactly, including las-sendas (a single-facility alias, where a sibling
filter should be a no-op) going 51 -> 0.

Do not guess a third time. This dumps:

  1. /alias/<alias>/facilities — every facility's id, courseId and name, so
     the integer -> Mongo-id mapping is on record;
  2. the BARE per-alias call — how many slots, which distinct courseIds and
     facilityIds appear, and the complete key list of one slot;
  3. the PINNED call — the same, to establish whether kenna filters
     server-side (if it does, the client filter is belt-and-braces on that
     path and only the bare fallback needs one).

Three aliases: one shared by four munis, one single-course, one that has been
flaky, so the shared-alias case and the simple case are both covered.

Public endpoints, report only. Nothing here edits the CSV, the registry, or
D1. No credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teeitup import TeeItUpAdapter  # noqa: E402

DATE = dt.date.today() + dt.timedelta(days=1)

# (alias, pinned facility_id, why)
TARGETS = [
    ("city-of-phoenix-golf-courses", "287", "four munis share this alias"),
    ("las-sendas-golf-club", "21", "one course; a sibling filter is a no-op"),
    ("golden-hills-golf-club", "1295", "alias fixed in 79e7379"),
]


def dump_slots(label: str, data, pinned: str) -> None:
    blocks = data if isinstance(data, list) else [data]
    slots = [s for b in blocks for s in ((b or {}).get("teetimes", []) or [])]
    print(f"    {label}: {len(slots)} slots")
    if not slots:
        keys = sorted((blocks[0] or {}).keys()) if blocks else []
        print(f"        response top-level keys: {keys}")
        return
    cids = Counter(str(s.get("courseId")) for s in slots)
    fids = Counter(str(s.get("facilityId")) for s in slots)
    print(f"        distinct courseId  : {dict(cids)}")
    print(f"        distinct facilityId: {dict(fids)}")
    hit = sum(n for k, n in list(cids.items()) + list(fids.items())
              if k == pinned)
    print(f"        slots whose courseId or facilityId equals the pinned "
          f"{pinned!r}: {hit}  <- what the current filter keeps")
    print(f"        one slot's keys: {sorted(slots[0].keys())}")
    print(f"        one slot: {json.dumps(slots[0])[:400]}")


def main() -> None:
    print("diag_kenna_slots: the slot shape the sibling filter is matching on")
    print(f"date probed: {DATE.isoformat()}")
    print("Report only. Nothing here edits the CSV, the registry, or D1.")
    ad = TeeItUpAdapter()

    for alias, pinned, why in TARGETS:
        print("\n" + "=" * 72)
        print(f"{alias}   pinned facility_id={pinned}   [{why}]")
        print("=" * 72)

        try:
            fac = ad.discover_facilities(alias)
        except Exception as exc:  # noqa: BLE001
            print(f"    facilities raised {type(exc).__name__}: {str(exc)[:80]}")
            fac = []
        print(f"    /alias/<alias>/facilities: {len(fac)} facilities")
        for f in fac:
            if isinstance(f, dict):
                print(f"        id={f.get('id')!r}  courseId={f.get('courseId')!r}"
                      f"  name={f.get('name')!r}  tz={f.get('timeZone')!r}")
        sys.stdout.flush()

        for shape, fids in (("BARE  (no facilityIds)", None),
                            (f"PINNED (facilityIds={pinned})", pinned)):
            try:
                data = ad._teetimes(alias, DATE, fids)
            except Exception as exc:  # noqa: BLE001
                print(f"    {shape}: raised {type(exc).__name__}: {str(exc)[:80]}")
                continue
            dump_slots(shape, data, pinned)
            sys.stdout.flush()

    print("\n" + "=" * 72)
    print("READ THIS BEFORE CHANGING THE FILTER")
    print("=" * 72)
    print("  * If no slot carries an integer facilityId, the filter must map "
          "the pinned integer to the facility's courseId first, via the "
          "facilities list, and match on that.")
    print("  * If PINNED returns fewer slots than BARE with only one distinct "
          "courseId, kenna filters server-side and the client filter is only "
          "needed on the bare fallback path.")
    print("  * If a facilities lookup fails, dropping every slot is the wrong "
          "default; so is keeping every slot on a shared alias. One distinct "
          "courseId in the response means it is unambiguous and safe to keep.")
    print("\ndone")


if __name__ == "__main__":
    main()
