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
    p.add_argument("--merge", action="store_true",
                   help="update only the slugs seen in --outputs, keeping every "
                        "other course's window and the file's as_of stamp")
    p.add_argument("--source", default="",
                   help="with --merge, a label recorded under sources[] so each "
                        "contributor's freshness is visible")
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
        # aggregate.py writes `courses_empty`, which d1.sync also consumes to
        # deactivate rows. browser_teeitup.py writes the same information as
        # `courses_empty_observed` precisely so it does NOT feed that path
        # while these windows are still being validated. Both mean "answered
        # cleanly with zero rows" and both are equally good evidence here.
        empties = set(doc.get("courses_empty")
                      or doc.get("courses_empty_observed") or [])
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

    # MERGE MODE exists because the window file has more than one contributor.
    # scrape-far.yml derives windows for the PLAIN platforms only — it excludes
    # clubprophet, ezlinks, golfnow, clubcaddie, supersaas, golfwithaccess,
    # teeitup, totale, trutee and teesnap, because separate workflows own those.
    # So 892 of 1,158 courses were never in this file at all, every browser
    # platform among them, and "unknown course -> eligible" meant they were
    # scraped on every far date forever. A browser tier can now contribute its
    # own courses' windows without touching anyone else's.
    #
    # as_of is NOT restamped on a merge. It is the plain far tier's own "have I
    # run a full sweep today" flag (scrape-far.yml's plan job reads it), and a
    # merging workflow stamping it would silently suppress that full sweep —
    # which is the one thing that keeps the plain windows honest.
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc)
                          .isoformat(timespec="seconds"),
        "as_of": dt.date.today().isoformat(),
        "floor": FLOOR, "grace": GRACE,
        "windows": windows,
    }
    if a.merge:
        prior = {}
        if out.is_file():
            try:
                prior = json.loads(out.read_text())
            except Exception as e:  # noqa: BLE001
                raise SystemExit(f"--merge given but {out} is unreadable ({e}); "
                                 "refusing to overwrite it with a partial file")
        merged = dict(prior.get("windows") or {})
        merged.update(windows)                     # only the slugs we just saw
        payload["windows"] = merged
        # Absent as_of means "no full plain sweep yet", which reads as stale
        # everywhere it matters — the conservative direction.
        payload["as_of"] = prior.get("as_of", "")
        payload["floor"] = prior.get("floor", FLOOR)
        payload["grace"] = prior.get("grace", GRACE)
        sources = dict(prior.get("sources") or {})
        if a.source:
            sources[a.source] = {"as_of": dt.date.today().isoformat(),
                                 "courses": len(windows)}
        if sources:
            payload["sources"] = sources
        print(f"merge: {len(windows)} slugs from this sweep into "
              f"{len(prior.get('windows') or {})} existing -> {len(merged)}")
        windows = merged
    out.write_text(json.dumps(payload, indent=1, sort_keys=True))
    caps = sum(1 for w in windows.values()
               if w["max_offset_with_rows"] < w["checked_through"])
    print(f"wrote {out}: {len(windows)} courses, "
          f"{caps} with a window shorter than checked range")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
