"""Geocode every directory venue that lacks a venue_geo row — per course.

The worker attaches each course's lat/lng by LEFT JOINing a `venue_geo`
(`venue_id -> lat, lng`) table onto its results, but that table was only ever
populated ad-hoc (CO / AZ / VA served courses). This backfills the rest — FL,
MD, UT and every book-on-site directory-only venue — so `venue_geo` covers the
whole directory and the "near me" filter works for every course, not just the
few states someone geocoded by hand.

Source of truth: `directory.json` (all 1600 venues, each with venue_id / name /
city / state). For every venue NOT already in `venue_geo` it queries Nominatim
(OpenStreetMap) for the COURSE itself — "<name>, <city>, <state>, USA" — and
falls back to the city/state centroid only when the named course can't be found.
Each resolved row is written immediately, so the run is:

  * idempotent  — already-geocoded venue_ids are skipped, re-running is a no-op
  * resumable   — a rate-limit / timeout just means the next run picks up where
                  this one stopped (rows already committed stay committed)

Runs on a GitHub runner: the dev sandbox's egress proxy blocks BOTH Nominatim
and the Cloudflare D1 API, and D1 writes need the CLOUDFLARE_* secrets.

    python -m scripts.geocode_venues                 # geocode all missing
    python -m scripts.geocode_venues --limit 300     # one batch (be polite)
    python -m scripts.geocode_venues --states FL,UT  # only these states
    python -m scripts.geocode_venues --local test.db # dev, against SQLite
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

from scraper.d1 import D1Rest, HttpBackend, SqliteLocal

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy: a genuine, identifying User-Agent and <= 1 req/sec.
UA = "onetee-geocoder/1.0 (https://www.oneteeapp.com golf tee-time directory)"
DELAY = 1.1          # seconds between Nominatim calls (policy: max ~1/sec)
DIRECTORY = "directory.json"


def ensure_table(db) -> None:
    db.executescript(
        "CREATE TABLE IF NOT EXISTS venue_geo ("
        "venue_id TEXT PRIMARY KEY, lat REAL, lng REAL)")
    # Best-effort audit column so a later pass can tell a precise course hit from
    # a coarse city-centre fallback. No-ops if it already exists / on old D1.
    try:
        db.execute("ALTER TABLE venue_geo ADD COLUMN source TEXT")
    except Exception:  # noqa: BLE001
        pass


def already_geocoded(db) -> set[str]:
    return {r["venue_id"] for r in db.execute("SELECT venue_id FROM venue_geo")}


def _query(session: requests.Session, q: str) -> tuple[float, float] | None:
    for attempt in range(3):
        try:
            r = session.get(
                NOMINATIM,
                params={"q": q, "format": "json", "limit": 1,
                        "countrycodes": "us"},
                headers={"User-Agent": UA}, timeout=25)
            if r.status_code == 429:          # rate-limited: back off, retry
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            hits = r.json()
            if hits:
                return float(hits[0]["lat"]), float(hits[0]["lon"])
            return None
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(2 * (attempt + 1))
    return None


def geocode(session: requests.Session, name: str, city: str,
            state: str) -> tuple[float, float, str] | None:
    """Course-precise first; city/state centroid only as a fallback."""
    name = (name or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    tried = []
    if name:
        # A venue named "Foo (Bar State Park)" geocodes better without the
        # parenthetical, which is our disambiguator, not the course's name.
        clean = name.split(" (")[0].strip()
        tried.append((f"{clean}, {city}, {state}, USA", "nominatim-name"))
    if city:
        tried.append((f"{city}, {state}, USA", "nominatim-city"))
    for q, src in tried:
        hit = _query(session, q)
        time.sleep(DELAY)
        if hit:
            return hit[0], hit[1], src
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backfill venue_geo for the directory")
    p.add_argument("--local", metavar="SQLITE_FILE",
                   help="run against a local SQLite file instead of D1")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after N geocodes this run (0 = all missing)")
    p.add_argument("--states", default="",
                   help="comma-separated states to restrict to (e.g. FL,UT,MD)")
    p.add_argument("--directory", default=DIRECTORY)
    a = p.parse_args(argv)

    if a.local:
        db = SqliteLocal(a.local)
    elif os.environ.get("VPS_ONLY") == "1":
        # Same cutover switch as scraper.d1's CLI: geocodes land in the VPS
        # Postgres venue_geo, not the retired D1.
        db = HttpBackend()
        print("VPS-ONLY backend (D1 disabled)", file=sys.stderr)
    else:
        db = D1Rest()
    ensure_table(db)
    have = already_geocoded(db)

    with open(a.directory) as fh:
        courses = json.load(fh)["courses"]
    want_states = {s.strip().upper() for s in a.states.split(",") if s.strip()}

    missing = [c for c in courses
               if c.get("venue_id") and c["venue_id"] not in have
               and (not want_states or c.get("state") in want_states)]
    print(f"directory={len(courses)}  already_geocoded={len(have)}  "
          f"missing={len(missing)}"
          + (f"  (states={sorted(want_states)})" if want_states else ""),
          flush=True)

    session = requests.Session()
    done = name_hits = city_hits = fail = 0
    for c in missing:
        if a.limit and done >= a.limit:
            print(f"hit --limit {a.limit}, stopping (rest resume next run)",
                  flush=True)
            break
        res = geocode(session, c.get("name", ""), c.get("city", ""),
                      c.get("state", ""))
        if res:
            lat, lng, src = res
            # Written as a native upsert rather than SQLite's "INSERT OR
            # REPLACE", because the VPS /exec endpoint rewrites that form using
            # a hard-coded primary-key map (vps/api/server.mjs) that lists only
            # tee_times and sheet_freshness. venue_geo is absent from it, so the
            # rewrite produced "ON CONFLICT ()" and Postgres rejected every row.
            # ON CONFLICT DO UPDATE passes straight through, and SQLite has
            # supported the same syntax since 3.24, so the --local path is
            # unaffected. (server.mjs's map should still gain venue_geo; that
            # needs a VPS deploy, and this does not.)
            db.execute(
                "INSERT INTO venue_geo (venue_id, lat, lng, source) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "lat=EXCLUDED.lat, lng=EXCLUDED.lng, source=EXCLUDED.source",
                [c["venue_id"], lat, lng, src])
            if src == "nominatim-name":
                name_hits += 1
            else:
                city_hits += 1
        else:
            fail += 1
            print(f"  NO MATCH: {c.get('name')} / {c.get('city')}, "
                  f"{c.get('state')}", flush=True)
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(missing)}  precise={name_hits} "
                  f"city-fallback={city_hits} unresolved={fail}", flush=True)

    print(f"DONE: geocoded {done} venues this run — precise={name_hits} "
          f"city-fallback={city_hits} unresolved={fail}. "
          f"venue_geo now covers {len(have) + name_hits + city_hits} venues.",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
