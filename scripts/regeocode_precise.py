"""Replace city-centroid venue_geo rows with the course's real location.

`scripts/geocode_venues.py` asks Nominatim for the course by name and falls
back to the CITY CENTROID when the name does not resolve. That fallback fired
often enough to break the map: measured 2026-09-01 against directory.json,
359 of 1,759 venues (20%) sit on a point they share with another venue, and
every one of those groups is a city centre. Denver is the worst single case —
Bear Creek, Fox Hollow, Homestead, Lakewood CC, Overland Park, Park Hill and
The Ranch CC are all stacked on 39.73924,-104.98486, which is downtown Denver
and is not where any of them is. A stacked group cannot be pulled apart by
zooming (the cluster never splits, because the points are identical), so the
map shows "7 courses" and opens exactly one.

This pass re-resolves ONLY those bad rows, and it uses OpenStreetMap's actual
golf-course geometry rather than a place lookup:

  1. One Overpass query PER STATE for every `leisure=golf_course` object in it,
     with `out center` so ways and relations come back with a centroid. Ten
     states is ten requests total, versus one request per venue - Overpass is a
     shared volunteer service and per-venue querying would be abusive.
  2. Match each stranded venue to those courses by normalised name, requiring
     both a good similarity score AND a sane distance from the venue's city.
  3. Fall back to the venue's ZIP centroid, which is far tighter than a city
     centroid and at least separates courses in different parts of one metro.

A row is only rewritten when the new point is genuinely better, and rows that
`geocode_venues.py` resolved by name are never touched.

    python -m scripts.regeocode_precise --dry-run          # report, write nothing
    python -m scripts.regeocode_precise --states CO        # one state
    python -m scripts.regeocode_precise                    # all

Runs on a GitHub runner: the dev sandbox's egress proxy blocks Overpass,
Nominatim and the database alike.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import math
import os
import re
import sys
import time

import requests

from scraper.d1 import D1Rest, HttpBackend, SqliteLocal

OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "onetee-geocoder/2.0 (https://www.oneteeapp.com golf tee-time directory)"
DIRECTORY = "directory.json"

# How far a candidate may sit from the venue's own city before we disbelieve it.
# Metro golf is genuinely spread out - Denver's listed "Denver" courses run out
# to Lakewood and Westminster - but 60 km is well beyond any of that and still
# rejects a same-named course in another part of the state.
MAX_KM = 60.0
# Name similarity floor. 0.82 accepts "Bear Creek Golf Club" vs "Bear Creek
# Golf Course" and rejects "Bear Creek" vs "Beaver Creek".
MIN_RATIO = 0.82

# Words that carry no identifying signal in a golf course name.
NOISE = re.compile(
    r"\b(golf|course|club|links|country|the|at|of|a|an|and|resort|"
    r"municipal|muni|public|park)\b")


def norm(s: str) -> str:
    s = (s or "").lower()
    s = s.split(" (")[0]                    # drop our disambiguating parenthetical
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = NOISE.sub(" ", s)
    return " ".join(s.split())


def haversine_km(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


STATE_NAME = {
    "AK": "Alaska", "AZ": "Arizona", "CO": "Colorado", "DC": "District of Columbia",
    "FL": "Florida", "MD": "Maryland", "UT": "Utah", "VA": "Virginia",
    "VT": "Vermont", "WY": "Wyoming", "NJ": "New Jersey", "NM": "New Mexico",
}


def overpass_state(session: requests.Session, state: str) -> list[dict]:
    """Every leisure=golf_course in one state, as {name, lat, lng}."""
    area = STATE_NAME.get(state, state)
    q = (
        '[out:json][timeout:180];'
        f'area["ISO3166-2"="US-{state}"][admin_level=4]->.s;'
        '(nwr["leisure"="golf_course"](area.s););'
        'out center tags;'
    )
    for attempt in range(4):
        try:
            r = session.post(OVERPASS, data={"data": q},
                             headers={"User-Agent": UA}, timeout=200)
            if r.status_code in (429, 504):
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            out = []
            for e in r.json().get("elements", []):
                tags = e.get("tags") or {}
                nm = tags.get("name")
                lat = e.get("lat", (e.get("center") or {}).get("lat"))
                lng = e.get("lon", (e.get("center") or {}).get("lon"))
                if nm and lat is not None and lng is not None:
                    out.append({"name": nm, "lat": float(lat), "lng": float(lng)})
            print(f"  overpass {state} ({area}): {len(out)} named golf courses",
                  flush=True)
            return out
        except (requests.RequestException, ValueError) as e:  # noqa: BLE001
            print(f"  overpass {state} attempt {attempt + 1} failed: "
                  f"{type(e).__name__}", flush=True)
            time.sleep(15 * (attempt + 1))
    return []


def nominatim(session: requests.Session, q: str) -> tuple[float, float] | None:
    try:
        r = session.get(NOMINATIM,
                        params={"q": q, "format": "json", "limit": 1,
                                "countrycodes": "us"},
                        headers={"User-Agent": UA}, timeout=25)
        if r.status_code == 429:
            time.sleep(6)
            return None
        r.raise_for_status()
        hits = r.json()
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def best_osm_match(venue: dict, pool: list[dict],
                   anchor: tuple[float, float]) -> tuple[float, float, float] | None:
    """Closest good name match within MAX_KM of the venue's current anchor."""
    want = norm(venue.get("name", ""))
    if not want:
        return None
    best = None
    for cand in pool:
        km = haversine_km(anchor[0], anchor[1], cand["lat"], cand["lng"])
        if km > MAX_KM:
            continue
        got = norm(cand["name"])
        if not got:
            continue
        ratio = difflib.SequenceMatcher(None, want, got).ratio()
        # A containment match ("bear creek" inside "bear creek east west") is
        # as trustworthy as a high ratio and much commoner with course names.
        if want == got or want in got.split("  ") or (
                want and (want in got or got in want)):
            ratio = max(ratio, 0.95)
        if ratio < MIN_RATIO:
            continue
        score = (ratio, -km)
        if best is None or score > best[0]:
            best = (score, cand, km, ratio)
    if not best:
        return None
    _, cand, km, ratio = best
    return cand["lat"], cand["lng"], ratio


def load_geo(db) -> dict[str, dict]:
    rows = db.execute("SELECT venue_id, lat, lng, source FROM venue_geo")
    return {r["venue_id"]: r for r in rows}


def pick_targets(courses: list[dict], geo: dict[str, dict],
                 want_states: set[str]) -> list[dict]:
    """Venues whose point is a city centroid, or is shared with another venue.

    Two independent signals, because neither alone is complete: `source` marks
    the fallback only for rows written after the audit column existed, and a
    stacked coordinate catches the rest. A single course alone in its town can
    be on a centroid without sharing it with anybody.
    """
    stacked = collections.Counter()
    for c in courses:
        if c.get("lat") is not None and c.get("lng") is not None:
            stacked[(round(c["lat"], 5), round(c["lng"], 5))] += 1

    out = []
    for c in courses:
        vid = c.get("venue_id")
        if not vid:
            continue
        if want_states and c.get("state") not in want_states:
            continue
        src = (geo.get(vid) or {}).get("source") or ""
        shared = (c.get("lat") is not None
                  and stacked[(round(c["lat"], 5), round(c["lng"], 5))] > 1)
        if src == "nominatim-city" or not src or shared:
            # Never re-resolve something already pinned to the course by name
            # unless it is demonstrably stacked on top of another course.
            if src == "nominatim-name" and not shared:
                continue
            out.append(c)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Re-resolve city-centroid venue_geo rows to real courses")
    p.add_argument("--local", metavar="SQLITE_FILE")
    p.add_argument("--states", default="")
    p.add_argument("--directory", default=DIRECTORY)
    p.add_argument("--dry-run", action="store_true",
                   help="print what would change, write nothing")
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args(argv)

    if a.local:
        db = SqliteLocal(a.local)
    elif os.environ.get("VPS_ONLY") == "1":
        db = HttpBackend()
        print("VPS-ONLY backend (D1 disabled)", file=sys.stderr)
    else:
        db = D1Rest()

    with open(a.directory) as fh:
        courses = json.load(fh)["courses"]
    want_states = {s.strip().upper() for s in a.states.split(",") if s.strip()}

    geo = load_geo(db)
    targets = pick_targets(courses, geo, want_states)
    by_state = collections.defaultdict(list)
    for c in targets:
        by_state[c.get("state") or ""].append(c)
    print(f"directory={len(courses)}  venue_geo={len(geo)}  "
          f"needing a real fix={len(targets)}  states="
          f"{ {k: len(v) for k, v in sorted(by_state.items())} }", flush=True)

    session = requests.Session()
    zip_cache: dict[str, tuple[float, float] | None] = {}
    fixed = osm = zipc = skipped = 0
    done = 0

    for state, group in sorted(by_state.items()):
        pool = overpass_state(session, state) if state else []
        time.sleep(2)
        for c in group:
            if a.limit and done >= a.limit:
                print(f"hit --limit {a.limit}, stopping", flush=True)
                break
            done += 1
            anchor = (c.get("lat"), c.get("lng"))
            new = src = None
            if anchor[0] is not None and pool:
                hit = best_osm_match(c, pool, anchor)
                if hit:
                    new, src = (hit[0], hit[1]), "overpass-osm"
            if new is None and c.get("zip"):
                z = str(c["zip"]).strip()[:5]
                if z not in zip_cache:
                    zip_cache[z] = nominatim(session, f"{z}, USA")
                    time.sleep(1.2)
                if zip_cache[z]:
                    new, src = zip_cache[z], "zip-centroid"
            if new is None:
                skipped += 1
                print(f"  no fix: {c.get('name')} / {c.get('city')}, {state}",
                      flush=True)
                continue
            if anchor[0] is not None and haversine_km(
                    anchor[0], anchor[1], new[0], new[1]) < 0.05:
                skipped += 1          # already there; nothing gained
                continue
            print(f"  {src:14s} {c.get('name')[:44]:44s} "
                  f"{anchor[0]},{anchor[1]} -> {new[0]:.5f},{new[1]:.5f}",
                  flush=True)
            if src == "overpass-osm":
                osm += 1
            else:
                zipc += 1
            fixed += 1
            if not a.dry_run:
                db.execute(
                    "INSERT INTO venue_geo (venue_id, lat, lng, source) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (venue_id) DO UPDATE SET "
                    "lat=EXCLUDED.lat, lng=EXCLUDED.lng, source=EXCLUDED.source",
                    [c["venue_id"], new[0], new[1], src])

    verb = "would fix" if a.dry_run else "fixed"
    print(f"DONE: {verb} {fixed} of {len(targets)} — "
          f"osm={osm} zip-centroid={zipc} unresolved={skipped}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
