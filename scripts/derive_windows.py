"""Derive per-course booking windows from a full far-tier sweep's own output.

"Accurate 30-day inventory" has a trap in it: most courses do not publish a
tee sheet 30 days out. Trinidad opens ~2 days ahead; Desert Canyon's Quick18
shows 30; Teesnap tenants carry an explicit `advance` of 14. A far-tier scan
that hammers every course for every one of 23 dates spends most of its
requests asking questions the platform has already answered with "the sheet
is not open yet" — and, worse, the frontend cannot tell "not open yet" from
"sold out" without knowing the window.

This script measures the window from data the sweep already fetched, costing
zero extra requests: it reads the aggregate output documents of one FULL far
sweep (every course, offsets 8..30), and records, per course, the farthest
offset that returned rows and the farthest offset actually checked.

Merge rules, deliberately conservative:
  * A course with rows at offset N gets window >= N.
  * A course empty everywhere in the far sweep gets window = FLOOR (7): the
    near/mid tiers cover 0..7 unconditionally, so the far tier simply skips
    it — and the daily FULL sweep re-checks everything, so a course that
    OPENS a longer window is picked up within a day.
  * Windows only shrink on a full sweep's evidence, never on a partial one.

Output: probe-results/booking-windows.json
    {"generated_at": ..., "as_of": "YYYY-MM-DD",
     "floor": 7, "grace": 3,
     "windows": {"<slug>": {"max_offset_with_rows": N,
                             "checked_through": M}}}

Usage:
    python scripts/derive_windows.py --outputs 'output/tee_times_*.json' \
        --registry registry.json --out probe-results/booking-windows.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import pathlib
import zoneinfo

FLOOR = 7    # near+mid tiers always cover offsets 0..7
GRACE = 3    # far tier still scrapes window+GRACE days, so a window that
             # stretches by a day or two is caught between full sweeps

_STATE_TZ_FALLBACK = "America/Denver"


def _tz_for(course: dict) -> str:
    # Lazy import so this script works standalone too.
    try:
        from scraper.d1 import _STATE_TZ
        return _STATE_TZ.get(course.get("state"), _STATE_TZ_FALLBACK)
    except Exception:  # noqa: BLE001
        return _STATE_TZ_FALLBACK


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outputs", default="output/tee_times_*.json",
                   help="glob of aggregate output docs from ONE full sweep")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--out", default="probe-results/booking-windows.json")
    a = p.parse_args()

    reg = json.loads(pathlib.Path(a.registry).read_text())["courses"]
    tz_by_slug = {c["slug"]: _tz_for(c) for c in reg}

    # slug -> {offset: had_rows}
    seen: dict[str, dict[int, bool]] = {}
    files = sorted(glob.glob(a.outputs))
    if not files:
        raise SystemExit(f"no aggregate outputs match {a.outputs!r} — "
                         "refusing to write a windows file from nothing")
    for f in files:
        doc = json.loads(pathlib.Path(f).read_text())
        date = dt.date.fromisoformat(doc["date"])
        with_rows = {t["course_slug"] for t in doc["tee_times"]}
        empties = set(doc.get("courses_empty", []))
        for slug in with_rows | empties:
            tz = zoneinfo.ZoneInfo(tz_by_slug.get(slug, _STATE_TZ_FALLBACK))
            offset = (date - dt.datetime.now(tz).date()).days
            seen.setdefault(slug, {})[offset] = (
                seen.get(slug, {}).get(offset, False) or slug in with_rows)

    windows = {}
    for slug, offs in sorted(seen.items()):
        with_rows = [o for o, ok in offs.items() if ok]
        windows[slug] = {
            "max_offset_with_rows": max(with_rows) if with_rows else FLOOR,
            "checked_through": max(offs),
        }

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc)
                          .isoformat(timespec="seconds"),
        "as_of": dt.date.today().isoformat(),
        "floor": FLOOR, "grace": GRACE,
        "windows": windows,
    }, indent=1, sort_keys=True))
    caps = sum(1 for w in windows.values()
               if w["max_offset_with_rows"] < w["checked_through"])
    print(f"wrote {out}: {len(windows)} courses, "
          f"{caps} with a window shorter than checked range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
