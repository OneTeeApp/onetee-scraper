"""Which courses are worth scraping for a given far-out date?

Reads probe-results/booking-windows.json (written by derive_windows.py after
each FULL far sweep) and prints a comma-joined slug list for --date, suitable
for aggregate.py's --courses flag. Printing contract, consumed by the far
workflow:

    ALL    no windows file yet (bootstrap) -> scrape everything, no --courses
    NONE   windows known and nobody's window reaches this date -> skip date
    a,b,c  the eligible slugs

Eligibility is deliberately generous:
  * unknown course (not in the windows file, e.g. newly added) -> eligible
  * offset <= max_offset_with_rows + grace                     -> eligible
  * offset beyond what the sweep ever checked                  -> eligible
    (never treat "we did not look" as "there is nothing")
  * a stale windows file (as_of older than --max-age-days)     -> ALL

Offset is computed against the EARLIEST local today across the timezones the
registry covers — the conservative direction: a smaller offset makes more
courses eligible, never fewer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper.dates import covered_timezones  # noqa: E402
import zoneinfo  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", required=True)
    p.add_argument("--windows", default="probe-results/booking-windows.json")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--max-age-days", type=int, default=3,
                   help="windows file older than this is ignored (-> ALL)")
    p.add_argument("--platform",
                   help="only consider courses on this platform. A per-platform "
                        "caller wants a per-platform answer: without it the whole "
                        "registry comes back and the caller passes ~1,100 slugs "
                        "on argv to a scraper that owns 384 of them.")
    p.add_argument("--source",
                   help="judge staleness by sources[<name>].as_of instead of the "
                        "file's top-level as_of. The top-level stamp belongs to "
                        "scrape-far.yml's plain sweep; a browser tier that trusted "
                        "it would silently stop pruning whenever the PLAIN tier "
                        "stalled, and keep pruning on its own stale windows if it "
                        "stalled itself. Each contributor should be judged on its "
                        "own freshness.")
    a = p.parse_args()

    wpath = pathlib.Path(a.windows)
    if not wpath.exists():
        print("ALL")
        return 0
    w = json.loads(wpath.read_text())
    stamp = w.get("as_of")
    if a.source:
        stamp = (w.get("sources") or {}).get(a.source, {}).get("as_of") or ""
    try:
        as_of = dt.date.fromisoformat(stamp)
    except (TypeError, ValueError):
        # No usable stamp — never derived, or a merge wrote an empty one. That
        # is ignorance, and ignorance reads as "scrape it".
        print("ALL")
        return 0
    if (dt.date.today() - as_of).days > a.max_age_days:
        print("ALL")
        return 0

    date = dt.date.fromisoformat(a.date)
    today = min(dt.datetime.now(zoneinfo.ZoneInfo(tz)).date()
                for tz in covered_timezones(a.registry))
    offset = (date - today).days
    grace = int(w.get("grace", 3))

    reg = json.loads(pathlib.Path(a.registry).read_text())["courses"]
    if a.platform:
        reg = [c for c in reg if c.get("platform") == a.platform]
    eligible = []
    for c in reg:
        win = w["windows"].get(c["slug"])
        if win is None:
            eligible.append(c["slug"])                    # new course
        elif offset <= win["max_offset_with_rows"] + grace:
            eligible.append(c["slug"])                    # inside window
        elif offset > win["checked_through"]:
            eligible.append(c["slug"])                    # never looked there

    print(",".join(sorted(set(eligible))) if eligible else "NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
