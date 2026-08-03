"""Generate realistic SIMULATED tee-time data in the exact schema the live
scraper emits, for demo/front-end development when network access to booking
APIs is unavailable. Every record is flagged "simulated": true.

Usage: python -m scraper.gen_sample --date 2026-07-23 --out output/tee_times.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import random

from .aggregate import load_registry

# rough public green-fee ranges (USD, 18 holes, prime summer) by course slug
PRICE_HINTS = {
    "breckenridge": (95, 145), "vail-golf-club": (110, 165),
    "aspen-golf-club": (90, 180), "glenwood-springs": (55, 75),
    "todd-creek": (55, 85), "battlement-mesa": (45, 70),
    "adobe-creek-national": (40, 60), "bella-rosa": (35, 55),
    "chipeta": (30, 45), "patty-jewett": (35, 55), "valley-hi": (35, 55),
    "antler-creek": (40, 65), "buffalo-run": (45, 70),
    "colorado-national": (55, 90), "commonground": (50, 80),
    "riverdale": (40, 70), "spring-valley": (35, 60),
    "steamboat-golf-club": (50, 75), "fossil-trace": (75, 115),
    "haymaker": (75, 120), "indian-peaks": (45, 75), "indian-tree": (40, 65),
    "red-hawk-ridge": (55, 90), "cattail-creek": (25, 40),
    "granby-ranch": (60, 95), "highland-meadows": (45, 70),
    "collindale": (40, 60), "applewood": (45, 70), "eaglevail": (75, 130),
    "denver-golf": (40, 75), "coal-creek": (40, 65),
    "meadows-foothills": (40, 70),
}


def generate(date: dt.date, seed: int = 42) -> dict:
    rng = random.Random(seed)
    registry = load_registry(pathlib.Path(__file__).parent.parent / "registry.json")
    tee_times = []
    for course in registry:
        if course["platform"] in ("golfnow", "ezlinks") or \
                course["platform"].startswith("other:"):
            continue  # honest: no adapter can fetch these yet
        lo, hi = PRICE_HINTS.get(course["slug"], (40, 70))
        # first tee ~6:30am, last ~5:50pm, 8-12 min intervals; some slots taken
        t = dt.datetime.combine(date, dt.time(6, 30))
        end = dt.datetime.combine(date, dt.time(17, 50))
        while t < end:
            if rng.random() < 0.55:  # ~55% of slots still open
                hour_factor = 1.0 if 8 <= t.hour < 14 else 0.8  # twilight cheaper
                base = rng.uniform(lo, hi) * hour_factor
                tee_times.append({
                    "course_slug": course["slug"],
                    "course_name": course["name"],
                    "city": course.get("city", ""),
                    # state/venue_id/course_label/source_role mirror the live
                    # doc shape. Omitting them minted NULL-state rows if a
                    # sample was ever pushed at a real DB — the exact "AZ
                    # ghosts filed under CO" class deactivate_unknown_slugs
                    # documents.
                    "state": course.get("state", ""),
                    "venue_id": course.get("venue_id") or course["slug"],
                    "source_role": course.get("source_role", "primary"),
                    "course_label": "",
                    "platform": course["platform"],
                    "teetime": t.isoformat(),
                    "holes": [18] if rng.random() < 0.7 else [9, 18],
                    "open_spots": rng.choice([1, 2, 2, 3, 3, 4, 4, 4]),
                    "price_min": round(base, 2),
                    "price_max": round(base * rng.uniform(1.0, 1.35), 2),
                    "currency": "USD",
                    "booking_url": course.get("booking_url", ""),
                    "simulated": True,
                })
            t += dt.timedelta(minutes=rng.choice([8, 9, 10, 12]))
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "simulated": True,
        "courses_queried": len(registry),
        "courses_ok": len({t["course_slug"] for t in tee_times}),
        "tee_times": tee_times,
        "errors": [],
    }


def main() -> None:
    import zoneinfo
    p = argparse.ArgumentParser()
    # Denver-local default, same reasoning as scraper/aggregate.py: a UTC
    # runner's `date.today()` is already tomorrow from ~5pm Mountain.
    p.add_argument("--date", default=(
        dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()
        + dt.timedelta(days=2)).isoformat())
    p.add_argument("--out", default="output/tee_times.json")
    a = p.parse_args()
    doc = generate(dt.date.fromisoformat(a.date))
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    print(f"wrote {out}: {len(doc['tee_times'])} simulated tee times "
          f"across {doc['courses_ok']} courses")


if __name__ == "__main__":
    main()
