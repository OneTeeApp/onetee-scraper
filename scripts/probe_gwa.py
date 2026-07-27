"""Verify the Golf With Access adapter end to end, from CI.

The dev sandbox's egress proxy 403s golfwithaccess.com, so the adapter can only
be exercised against the live API from GitHub Actions. This runs the REAL
production path — aggregate.fetch_course() — for all 11 golfwithaccess registry
courses across three spread dates, and prints a per-course verdict.

Empty is not failure here: Poston Butte, Las Colinas and Pusch Ridge were
measured returning zero across the whole 30-day horizon at build time (summer /
Access-only channels), so a clean empty for them is expected and recorded as
such rather than as a fault. What would be a real failure is an exception, or
the id-mismatch guard firing (the adapter refusing a wrong course's sheet).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper.aggregate import fetch_course, load_registry  # noqa: E402

DATE_OFFSETS = (2, 4, 7)
KNOWN_EMPTY = {"poston-butte-golf-club", "las-colinas-golf-club",
               "pusch-ridge-golf-course"}


def main() -> int:
    reg = [c for c in load_registry("registry.json")
           if c["platform"] == "golfwithaccess"]
    today = dt.date.today()
    dates = [today + dt.timedelta(days=d) for d in DATE_OFFSETS]

    print(f"probe_gwa: {len(reg)} golfwithaccess courses over "
          f"{', '.join(d.isoformat() for d in dates)}\n")

    out = {"generated_at": dt.datetime.now(dt.timezone.utc)
           .isoformat(timespec="seconds"),
           "dates": [d.isoformat() for d in dates], "courses": []}
    served = errored = 0
    for c in sorted(reg, key=lambda x: x["slug"]):
        rec = {"slug": c["slug"], "days": {}}
        total = 0
        for d in dates:
            r = fetch_course(c, d)
            if r.ok:
                rec["days"][d.isoformat()] = len(r.tee_times)
                total += len(r.tee_times)
            else:
                rec["days"][d.isoformat()] = f"ERR {str(r.error)[:80]}"
        rec["total"] = total
        if any(isinstance(v, str) for v in rec["days"].values()):
            rec["verdict"] = "errored"
            errored += 1
        elif total > 0:
            rec["verdict"] = "serving"
            served += 1
        elif c["slug"] in KNOWN_EMPTY:
            rec["verdict"] = "empty_expected"
        else:
            rec["verdict"] = "empty_unexpected"
        out["courses"].append(rec)
        print(f"  {c['slug']:<44} {rec['verdict']:<16} "
              f"total={total}  {rec['days']}")
        sys.stdout.flush()

    print(f"\n  serving={served}  errored={errored}  "
          f"of {len(reg)} courses")
    pathlib.Path("probe-results").mkdir(exist_ok=True)
    pathlib.Path("probe-results/gwa.json").write_text(json.dumps(out, indent=1))
    print("wrote probe-results/gwa.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
