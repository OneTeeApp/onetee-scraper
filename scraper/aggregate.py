"""Tee-time aggregator CLI.

Usage:
    python -m scraper.aggregate --date 2026-07-24 [--platforms foreup,teeitup]
        [--courses breckenridge,vail-golf-club] [--out output/tee_times.json]
        [--registry registry.json] [--include-raw] [--workers 8]

Fetches every matching course concurrently, normalizes to the unified
TeeTime model, and writes one JSON document with data + per-course errors,
so partial failures never hide.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import pathlib
import sys

from .models import FetchResult
from .sharding import apply_shard, set_env_shard_count
from .adapters.base import Adapter, make_session
from .adapters.foreup import ForeUpAdapter
from .adapters.teeitup import TeeItUpAdapter
from .adapters.chronogolf import ChronogolfAdapter
from .adapters.clubprophet import ClubProphetAdapter
from .adapters.quick18 import Quick18Adapter
from .adapters.teesnap import TeesnapAdapter
from .adapters.clubcaddie import ClubCaddieAdapter
from .adapters.foretees import ForeTeesAdapter
from .adapters.ezlinks import EZLinksAdapter
from .adapters.golfwithaccess import GolfWithAccessAdapter
from .adapters.rguest import RGuestAdapter
from .adapters.courseco import CourseCoAdapter
from .adapters.experimental import (
    MemberSportsAdapter, GolfNowAdapter, OtherAdapter, TotaleAdapter,
)

log = logging.getLogger("teetime")

ADAPTERS: dict[str, type[Adapter]] = {
    "foreup": ForeUpAdapter,
    "teeitup": TeeItUpAdapter,
    "chronogolf": ChronogolfAdapter,
    "clubprophet": ClubProphetAdapter,
    "membersports": MemberSportsAdapter,
    "clubcaddie": ClubCaddieAdapter,
    "golfnow": GolfNowAdapter,
    "ezlinks": EZLinksAdapter,
    "teesnap": TeesnapAdapter,
    "quick18": Quick18Adapter,
    "foretees": ForeTeesAdapter,
    "golfwithaccess": GolfWithAccessAdapter,
    "totale": TotaleAdapter,          # browser-only; see scraper/browser_totale.py
    "rguest": RGuestAdapter,
    "courseco": CourseCoAdapter,   # Total-e's replacement; plain HTTP
}


def get_adapter(platform: str):
    if platform.startswith("other:"):
        return OtherAdapter
    return ADAPTERS.get(platform)


def load_registry(path: str | pathlib.Path) -> list[dict]:
    with open(path) as f:
        return json.load(f)["courses"]


def fetch_course(course: dict, date: dt.date) -> FetchResult:
    adapter_cls = get_adapter(course["platform"])
    if adapter_cls is None:
        return FetchResult(course["slug"], course["platform"], False,
                           error=f"no adapter for platform {course['platform']}")
    try:
        tee_times = adapter_cls().fetch(course, date)
        return FetchResult(course["slug"], course["platform"], True, tee_times)
    except Exception as e:  # noqa: BLE001 — aggregator must survive any course
        return FetchResult(course["slug"], course["platform"], False,
                           error=f"{type(e).__name__}: {e}")


def run(date: dt.date, registry_path: str, out_path: str,
        platforms: set[str] | None, courses: set[str] | None,
        include_raw: bool, workers: int, exclude: set[str] | None = None,
        shard: str | None = None, states: set[str] | None = None) -> dict:
    set_env_shard_count(shard)          # so per-host throttles pace at 1/N
    registry = load_registry(registry_path)
    targets = [c for c in registry
               if (not platforms or c["platform"] in platforms)
               and (not exclude or c["platform"] not in exclude)
               and (not courses or c["slug"] in courses)
               # State filter is for isolating ONE state while debugging, and
               # for per-state health gates. It is deliberately NOT how the
               # hourly scan partitions work: --shard hashes courses evenly,
               # whereas states differ ~10x in size, so state-sharding the
               # scan would leave one runner doing California while the rest
               # idle. Divide the REPORTING by state; divide the RUNTIME by hash.
               and (not states or c.get("state") in states)]
    targets = apply_shard(targets, shard)
    log.info("fetching %d courses for %s%s", len(targets), date,
             f" (shard {shard})" if shard else "")

    results: list[FetchResult] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_course, c, date): c for c in targets}
        for fut in cf.as_completed(futs):
            res = fut.result()
            results.append(res)
            log.info("  %-28s %s", res.course_slug,
                     f"{len(res.tee_times)} times" if res.ok else f"ERROR {res.error}")

    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "courses_queried": len(targets),
        "courses_ok": sum(r.ok for r in results),
        "tee_times": [t.to_dict(include_raw)
                      for r in results for t in r.tee_times],
        # Courses that answered CLEANLY with zero rows for this date. sync()
        # deactivates these for this date (the day sold out, or the booking
        # window closed). Two guards on what counts as a trustworthy empty:
        #
        #   1. An errored course is never here - its absence is ignorance, not
        #      evidence (handled in sync via the errored set too).
        #   2. PLATFORM-LIVENESS: a course's empty only counts if at least one
        #      OTHER course on the same platform served rows this run. This is
        #      the fix for the far-horizon collapse (2026-07-27): kenna
        #      soft-throttles our data-center IP and returns empty-200 (not
        #      429) for a stretch of far dates, so a whole platform can go
        #      "clean empty" at once. Trusting that deactivated real inventory
        #      a prior sweep had captured - teeitup vanished from ~day 12-22.
        #      73 courses do not sell out in the same second; a serving sibling
        #      proves the platform is actually answering, so this course's
        #      empty is a real sell-out rather than a throttle. If the whole
        #      platform is empty this run, NONE are deactivated and the last
        #      good capture survives until a run that truly reads the sheet.
        "courses_empty": sorted(
            r.course_slug for r in results
            if r.ok and not r.tee_times
            and r.platform in {x.platform for x in results
                               if x.ok and x.tee_times}),
        "errors": [{"course": r.course_slug, "platform": r.platform,
                    "error": r.error} for r in results if not r.ok],
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors)",
             out, len(doc["tee_times"]), len(doc["errors"]))
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aggregate golf tee times")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=2)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--out", default="output/tee_times.json")
    p.add_argument("--platforms", help="comma-separated platform filter")
    p.add_argument("--exclude", help="comma-separated platforms to skip")
    p.add_argument("--courses", help="comma-separated course-slug filter")
    p.add_argument("--include-raw", action="store_true")
    p.add_argument("--workers", type=int, default=5)  # polite: retry handles 429
    p.add_argument("--shard", help="i/N — process a deterministic 1/N slice")
    p.add_argument("--states", help="comma-separated state filter, e.g. CO,AZ")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if not a.verbose else logging.DEBUG,
                        format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out,
        set(a.platforms.split(",")) if a.platforms else None,
        set(a.courses.split(",")) if a.courses else None,
        a.include_raw, a.workers,
        set(a.exclude.split(",")) if a.exclude else None,
        a.shard,
        {s.strip().upper() for s in a.states.split(",")} if a.states else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
