"""End-to-end verification: run the real EZLinks + GolfNow browser fetchers
for tomorrow and print a compact per-course summary. Used by the diagnostic
workflow to confirm the production pipelines work from GitHub before trusting
the hourly schedule. Not part of the scrape path.
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, os.getcwd())  # allow `python scripts/…` to import the package

from scraper import browser_ezlinks, browser_golfnow  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)


def summarize(name: str, doc: dict) -> None:
    times = doc.get("tee_times") or []
    counts = collections.Counter(t["course_slug"] for t in times)
    print(f"\n== {name}: queried {doc.get('courses_queried')} "
          f"ok {doc.get('courses_ok')} times {len(times)} "
          f"errors {len(doc.get('errors') or [])} ==", flush=True)
    for slug, n in sorted(counts.items()):
        sample = next((t for t in times if t["course_slug"] == slug), {})
        print(f"  {slug:<40} {n:>4}  e.g. {sample.get('teetime')} "
              f"${sample.get('price_min')}", flush=True)
    for e in (doc.get("errors") or []):
        print(f"  ERR {e['course']:<36} {e['error']}", flush=True)


def main() -> None:
    date = dt.date.today() + dt.timedelta(days=1)
    ez = browser_ezlinks.run(date, "registry.json", "output/ez_verify.json")
    gn = browser_golfnow.run(date, "registry.json", "output/gn_verify.json")
    print(f"VERIFY for {date.isoformat()}", flush=True)
    summarize("EZLINKS", ez)
    summarize("GOLFNOW", gn)


if __name__ == "__main__":
    main()
