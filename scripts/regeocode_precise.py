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
# to Lakewood and Westminster - but the all-states dry run put the 90th
# percentile of accepted moves at 15 km, and the single worst match in it was
# FL's "Oceans Golf Club" (Daytona Beach Shores) grabbing a same-named course
# 51 km inland. 35 km keeps essentially every genuine metro move and rejects
# that; anything rejected here falls back to the ZIP centroid, which is the
# safer answer anyway.
MAX_KM = 35.0
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


def _n(s: str) -> str:
    """Normalise WITHOUT dropping the parenthetical."""
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(NOISE.sub(" ", s).split())


def name_variants(name: str) -> list[str]:
    """Every form of a venue name worth matching against OSM.

    Our directory disambiguates sibling courses in a parenthetical or after a
    dash - "Sun City West (Deer Valley)", "Lely Resort Golf & Country Club -
    Flamingo Island Course" - but OSM usually indexes the course under the
    DISTINGUISHING part ("Deer Valley Golf Course", "Flamingo Island Course"),
    not the facility. Dropping the parenthetical, which is all norm() did, threw
    away the half OSM knows: in the 2026-09-01 all-states dry run that left 60%
    of Florida and 31% of Arizona unresolved while Colorado - whose names carry
    no parentheticals - resolved 112 of 112. So try the whole name, the
    parenthetical on its own, and the facility base, and keep the best.
    """
    out, seen = [], set()
    inner = re.findall(r"\(([^()]*)\)", name or "")
    tail = re.split(r"\s+[-\u2013\u2014]\s+", name or "")
    for cand in [_n(name), norm(name)] + [_n(x) for x in inner] + \
                ([_n(tail[-1])] if len(tail) > 1 else []):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def plain(s: str) -> str:
    """Lowercase, alphanumerics only - no noise-word stripping. Matches nz() in
    the site's map component, which is what decides what counts as one
    facility."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def facility_base(n: str) -> str:
    """"Wigwam Golf Club (Gold Course)" / "East Potomac - Blue Course" -> the
    facility. Mirrors facilityBase() in the site's map component so the two
    agree about what counts as one facility."""
    s = re.sub(r"\s*\([^()]*\)\s*$", "", n or "")
    return re.split(r"\s+[-\u2013\u2014]\s+", s)[0].strip()


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


def nominatim_near(session: requests.Session, q: str,
                   lat: float, lng: float,
                   box: float = 0.45) -> tuple[float, float] | None:
    """Nominatim restricted to a box around the venue's own city.

    The first-pass geocoder already tried "<name>, <city>, <state>" unbounded
    and fell through to the city centroid, so repeating that is pointless. A
    BOUNDED search is a different question - it asks Nominatim for anything
    matching the name inside this box - and it picks up courses indexed as POIs
    rather than as places, which is where most of the still-missing ones live.
    """
    try:
        r = session.get(NOMINATIM,
                        params={"q": q, "format": "json", "limit": 1,
                                "countrycodes": "us", "bounded": 1,
                                "viewbox": f"{lng - box},{lat + box},"
                                           f"{lng + box},{lat - box}"},
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
    wants = name_variants(venue.get("name", ""))
    if not wants:
        return None
    best = None
    for cand in pool:
        km = haversine_km(anchor[0], anchor[1], cand["lat"], cand["lng"])
        if km > MAX_KM:
            continue
        got = _n(cand["name"])
        if not got:
            continue
        want, ratio = max(
            ((w, difflib.SequenceMatcher(None, w, got).ratio()) for w in wants),
            key=lambda x: x[1])
        # OSM often carries a longer form of the same name ("Fox Hollow at
        # Lakewood Golf Course" for our "Fox Hollow Golf Course"), which scores
        # far below MIN_RATIO on raw similarity. Accept it only when EVERY word
        # of the shorter name appears in the longer one AND the shorter name has
        # at least two words. The two-word floor is what stops the failure this
        # rule caused on its first outing: a bare substring test matched "Aspen
        # Glen Club" to "Aspen Golf Club", because both reduce to something
        # containing "aspen", and it silently stacked two unrelated clubs on one
        # point - the exact defect this script exists to remove.
        a, b = want.split(), got.split()
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        if want == got or (len(short) >= 2 and set(short) <= set(long_)):
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
    fixed = osm = zipc = nearby = skipped = 0
    done = 0
    # GitHub's log viewer virtualises to a few dozen rendered lines, so a run
    # that moves a thousand coordinates cannot actually be reviewed from the
    # log. Collect every decision and write it to the job summary instead,
    # which renders in full on the run page.
    report: list[tuple[str, str, str, str]] = []
    contested: list[str] = []

    for state, group in sorted(by_state.items()):
        pool = overpass_state(session, state) if state else []
        time.sleep(2)

        # Resolve OSM claims for the whole state BEFORE writing anything.
        # Without this a single OSM course can be claimed by several unrelated
        # venues, which just rebuilds the stack this script exists to remove.
        # Two venues may legitimately share one OSM course when they are two
        # courses of the SAME facility (East Potomac Blue/Red/White is one
        # polygon in OSM), so the tie-break keeps every venue whose facility
        # base name matches the best claimant's and drops the rest to ZIP.
        proposals: dict[str, tuple[tuple[float, float], float]] = {}
        for c in group:
            anchor = (c.get("lat"), c.get("lng"))
            if anchor[0] is None or not pool:
                continue
            hit = best_osm_match(c, pool, anchor)
            if hit:
                proposals[c["venue_id"]] = ((hit[0], hit[1]), hit[2])
        claims: dict[tuple, list[str]] = collections.defaultdict(list)
        for vid, (pt, _r) in proposals.items():
            claims[(round(pt[0], 5), round(pt[1], 5))].append(vid)
        by_id = {c["venue_id"]: c for c in group}
        for pt, vids in claims.items():
            if len(vids) < 2:
                continue
            vids.sort(key=lambda v: -proposals[v][1])
            # Compare with plain normalisation, NOT norm(): norm() strips
            # "golf", "club", "country", "the", "at" as noise, which collapses
            # "Castle Pines Golf Club" and "Country Club at Castle Pines" to the
            # same string - two genuinely different Castle Rock clubs, which the
            # first all-states dry run duly stacked on one point. Same for
            # "St. Johns Golf Club" vs "St. Johns Golf & Country Club". Those
            # words are noise for MATCHING a name against OSM and load-bearing
            # for telling two clubs apart.
            keep_base = plain(facility_base(by_id[vids[0]].get("name", "")))
            for v in vids[1:]:
                if plain(facility_base(by_id[v].get("name", ""))) != keep_base:
                    msg = (f"{by_id[v].get('name')} ({state}) loses "
                           f"{pt[0]:.5f},{pt[1]:.5f} to "
                           f"{by_id[vids[0]].get('name')}")
                    print(f"  contested: {msg}", flush=True)
                    contested.append(msg)
                    proposals.pop(v, None)

        for c in group:
            if a.limit and done >= a.limit:
                print(f"hit --limit {a.limit}, stopping", flush=True)
                break
            done += 1
            anchor = (c.get("lat"), c.get("lng"))
            new = src = None
            if c["venue_id"] in proposals:
                new, src = proposals[c["venue_id"]][0], "overpass-osm"
            if new is None and anchor[0] is not None:
                q = f"{facility_base(c.get('name', ''))}, {c.get('state', '')}"
                hit = nominatim_near(session, q, anchor[0], anchor[1])
                time.sleep(1.2)          # Nominatim policy: <= 1 req/sec
                if hit and haversine_km(anchor[0], anchor[1],
                                        hit[0], hit[1]) <= MAX_KM:
                    new, src = hit, "nominatim-bounded"
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
                report.append((state, c.get("name", ""), "unresolved", ""))
                continue
            if anchor[0] is not None and haversine_km(
                    anchor[0], anchor[1], new[0], new[1]) < 0.05:
                skipped += 1          # already there; nothing gained
                continue
            moved = (haversine_km(anchor[0], anchor[1], new[0], new[1])
                     if anchor[0] is not None else 0.0)
            print(f"  {src:14s} {c.get('name')[:44]:44s} "
                  f"{anchor[0]},{anchor[1]} -> {new[0]:.5f},{new[1]:.5f}",
                  flush=True)
            report.append((state, c.get("name", ""), src,
                           f"{anchor[0]},{anchor[1]} → {new[0]:.5f},{new[1]:.5f}"
                           f" ({moved:.1f} km)"))
            if src == "overpass-osm":
                osm += 1
            elif src == "nominatim-bounded":
                nearby += 1
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
    summary = (f"{verb} {fixed} of {len(targets)} — osm={osm} "
               f"nominatim-bounded={nearby} zip-centroid={zipc} "
               f"unresolved={skipped} contested-dropped={len(contested)}")
    print(f"DONE: {summary}", flush=True)
    write_job_summary(a.dry_run, summary, report, contested)
    return 0


def write_job_summary(dry_run: bool, summary: str,
                      report: list[tuple[str, str, str, str]],
                      contested: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    head = "Dry run — nothing written" if dry_run else "Applied to venue_geo"
    lines = [f"## Re-geocode: {head}", "", summary, ""]
    if contested:
        lines += ["### Contested OSM matches (dropped, fell back to ZIP)", ""]
        lines += [f"- {c}" for c in contested] + [""]
    lines += ["### Every proposed move", "",
              "| State | Course | Source | Move |", "|---|---|---|---|"]
    # A 1 MB cap applies to the summary; 1,200 rows is comfortably inside it.
    for st, name, src, move in report[:1200]:
        safe = name.replace("|", "\\|")
        lines.append(f"| {st} | {safe} | {src} | {move} |")
    if len(report) > 1200:
        lines.append(f"| … | {len(report) - 1200} more rows | | |")
    try:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as e:  # noqa: BLE001
        print(f"  (could not write job summary: {e})", flush=True)
    return


if __name__ == "__main__":
    raise SystemExit(main())
