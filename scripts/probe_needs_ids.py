"""Ask the platforms whether `needs_ids` courses are actually missing anything.

WHY
---
Forty-eight registry courses sit in `needs_ids`, and 45 of them are chronogolf
(30) or foreup (15). Both groups are failed by build_registry's status chain
for a missing identifier — chronogolf for `club_uuid`, foreup for
`schedule_id`. Both adapters resolve that identifier THEMSELVES at runtime:

  ChronogolfAdapter.fetch()  -> discover(slug or club_id) resolves club_id,
                                affiliation_type_id and course_ids. It never
                                reads club_uuid. Nothing does. And
                                extract_ids() hardcodes club_uuid=None on every
                                chronogolf row, so the gate rejects 100% of them.
  ForeUpAdapter.fetch()      -> when schedule_id is absent it calls
                                discover_ids(course_id) and regexes the id out
                                of the booking page.

So the gate may be holding 45 courses out of the scrape for keys the scrape
does not need. "May be" is the point: the way to settle that is to ask the
platform, not to reason about the code, because the other possibility is real —
a chronogolf slug can resolve to an unclaimed directory listing that never
sells a tee time (confirmed for Lake Estes, Las Animas and Elmwood), and a
foreup booking page can have no schedule in it at all.

VERDICTS
--------
Every course lands in exactly one bucket, and a failure that could not be
classified is filed as `error` with the message rather than folded into a
verdict. A missing answer must not be recorded as a known one.

  ready       the platform gave us everything fetch() needs. The gate is
              wrong about this course and it can be scraped today.
  unclaimed   chronogolf only. The club resolves but online_booking_enabled is
              False — an unclaimed directory listing. This is a real fact about
              the course, not a missing id: no identifier we could capture
              would make it bookable, so `needs_ids` is the wrong label and it
              should be retagged rather than left looking like homework.
  no_aff      chronogolf: bookable club, no default_affiliation_type_id, so the
              marketplace call 422s. Contact-only club.
  no_courses  chronogolf: bookable club with zero online-bookable courses.
  no_schedule foreup: the booking page loads but carries no schedule_id.
  missing     the id we hold 404s. A genuine needs_ids — the captured id is
              wrong and a human has to find the right one.
  error       anything else, verbatim.

This is read-only over the network and writes one JSON file. It cannot run in
the dev sandbox, whose egress proxy 403s chronogolf.com, foreupsoftware.com and
*.cps.golf — hence .github/workflows/probe-needs-ids.yml, which runs it where
the scrape already runs and commits the result back.

  python3 scripts/probe_needs_ids.py [--out probe-results/needs-ids.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, ".")

from scraper.adapters.chronogolf import ChronogolfAdapter  # noqa: E402
from scraper.adapters.foreup import ForeUpAdapter  # noqa: E402


def probe_chronogolf(ad: ChronogolfAdapter, ids: dict) -> dict:
    key = ids.get("club_id") or ids.get("slug")
    if not key:
        return {"verdict": "missing", "detail": "no slug or club_id"}
    disc = ad.discover(str(key))
    row = {"club_id": disc["club_id"],
           "affiliation_type_id": disc["affiliation_type_id"],
           "course_ids": disc["course_ids"],
           "course_names": {str(k): v for k, v in disc["course_names"].items()}}
    if not disc["club_bookable"]:
        row["verdict"] = "unclaimed"
    elif not disc["affiliation_type_id"]:
        row["verdict"] = "no_aff"
    elif not disc["course_ids"]:
        row["verdict"] = "no_courses"
    else:
        row["verdict"] = "ready"
    return row


def probe_foreup(ad: ForeUpAdapter, ids: dict) -> dict:
    cid = ids.get("course_id")
    if not cid:
        return {"verdict": "missing", "detail": "no course_id"}
    found = ad.discover_ids(str(cid))
    row = {"schedule_ids": found.get("schedule_id") or [],
           "booking_classes": found.get("booking_class") or [],
           "teesheet_ids": found.get("teesheet_id") or []}
    row["verdict"] = "ready" if row["schedule_ids"] else "no_schedule"
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--out", default="probe-results/needs-ids.json")
    ap.add_argument("--platforms", default="chronogolf,foreup")
    ap.add_argument("--delay", type=float, default=1.0)
    a = ap.parse_args()

    want = {p.strip() for p in a.platforms.split(",") if p.strip()}
    with open(a.registry) as fh:
        reg = json.load(fh)["courses"]
    courses = [c for c in reg
               if c["status"] == "needs_ids" and c["platform"] in want]

    probes = {"chronogolf": (ChronogolfAdapter(), probe_chronogolf),
              "foreup": (ForeUpAdapter(), probe_foreup)}

    results = []
    for i, c in enumerate(courses):
        row = {"slug": c["slug"], "state": c["state"],
               "platform": c["platform"], "venue_id": c.get("venue_id"),
               "ids": c["ids"]}
        if i:
            time.sleep(a.delay)
        ad, fn = probes[c["platform"]]
        try:
            row.update(fn(ad, c["ids"]))
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            row["verdict"] = "missing" if "404" in msg else "error"
            row["detail"] = msg[:300]
        results.append(row)
        extra = ""
        if row["verdict"] == "ready":
            extra = (f"  club={row.get('club_id')} "
                     f"courses={len(row.get('course_ids') or [])}"
                     if c["platform"] == "chronogolf"
                     else f"  schedule={(row.get('schedule_ids') or [''])[0]}")
        print(f"{row['state']} {c['platform']:<11} {c['slug']:<44} "
              f"{row['verdict']}{extra}", flush=True)

    tally = Counter(r["verdict"] for r in results)
    by_plat = Counter((r["platform"], r["verdict"]) for r in results)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    for (p, v), n in sorted(by_plat.items()):
        print(f"  {p:<11} {v:<12} {n}")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump({"probed": len(results), "tally": dict(tally),
                   "results": results}, fh, indent=1)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
