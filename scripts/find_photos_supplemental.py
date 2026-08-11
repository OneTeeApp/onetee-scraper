#!/usr/bin/env python3
"""
Find photos for the courses that have no website worth crawling.

WHY THIS EXISTS
`find_course_photos.py` reads each course's own site. Thirty-six Colorado
courses defeat it: thirteen have no website at all, thirteen have one that will
not answer, and ten publish nothing usable. Those are mostly rural nine-holers,
and they are exactly the cards that will look broken.

This script asks somewhere else. It searches by the course's **coordinates**
rather than its name, because this directory has already been bitten by name
collisions — `mtgolfclub.com` is a Mountain Meadows in Pomona, California, not
the one in Red Feather Lakes. A lat/lng cannot be in the wrong state.

THE SOURCES, AND WHAT EACH ONE COSTS YOU

  wikimedia   Commons geosearch. No key, no cost, and every file states its
              licence. Coverage is thin for rural courses, but it is free to ask.

  flickr      Filtered to licences that permit commercial use. Much larger
              corpus than Commons, and golfers photograph golf courses.
              Needs FLICKR_API_KEY.

  brave       Brave's image search. Best hit rate of the three — and the one
              with no licence attached. Brave's terms are explicit that the API
              grants no rights to third-party content: you must satisfy
              yourself about the publisher's copyright. Results are therefore
              labelled `licence: unknown` and the picker says so.
              Needs BRAVE_API_KEY.

  usgs        The National Map's aerial imagery, framed on the course. Public
              domain, no key, and it has full coverage by construction — there
              is always an aerial. It is an overhead rather than a ground-level
              photo, so it is the last resort, not the first.

Every candidate carries its source and its licence so the picker can show both,
and so attribution can be rendered where a licence requires it.

USAGE
  python scripts/find_photos_supplemental.py --state CO            # every gap
  python scripts/find_photos_supplemental.py --only-file scripts/recrawl_ids.json
  python scripts/find_photos_supplemental.py --state CO --sources wikimedia,usgs

Reads and rewrites data/course_photos_full.json, appending candidates to the
rows that need them. It never overwrites a course's existing candidates and
never touches data/course_photos.json — choosing is still a human's job.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
from typing import Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This script needs `requests` (pip install requests).")


UA = "OneTeeBot/1.0 (+https://www.oneteeapp.com)"
TIMEOUT = 15

# Flickr licence ids that permit commercial use. The NonCommercial ones (2, 3,
# 6) are deliberately absent: OneTee is a commercial site, and "free to use"
# and "free to use in a business" are not the same sentence.
FLICKR_LICENCES = {
    "4": "CC BY 2.0 (credit required)",
    "5": "CC BY-SA 2.0 (credit required)",
    "7": "No known copyright restrictions",
    "9": "CC0 public domain",
    "10": "Public Domain Mark",
}

USGS_EXPORT = ("https://basemap.nationalmap.gov/arcgis/rest/services/"
               "USGSImageryOnly/MapServer/export")


# --------------------------------------------------------------------------

def usgs_url(lat: float, lng: float, span_m: int = 1400, size=(1000, 640)) -> str:
    """
    An aerial framed on the course.

    A golf course is roughly a kilometre across, so a 1.4 km box holds most of
    one with a margin. Longitude degrees shrink towards the poles, hence the
    cosine — without it a Colorado course comes out stretched sideways.
    """
    dlat = (span_m / 2) / 111_320.0
    dlng = dlat / max(0.2, math.cos(math.radians(lat)))
    # Match the box aspect to the image aspect, or ArcGIS letterboxes it.
    dlng *= (size[0] / size[1]) / (1.0)
    bbox = f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}"
    q = {
        "bbox": bbox, "bboxSR": "4326", "imageSR": "3857",
        "size": f"{size[0]},{size[1]}", "format": "jpg", "f": "image",
    }
    return USGS_EXPORT + "?" + urllib.parse.urlencode(q)


OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Every golf course in a state, fetched once. Keyed by state.
_state_courses: Dict[str, List[Dict]] = {}


def _norm(name: str) -> str:
    """Course names, reduced to the part that actually identifies them."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(golf|course|club|country|links|municipal|muni|the|at|and|"
               r"of|a|resort|ranch|park|recreation|district|cc|gc)\b", " ", n)
    return " ".join(n.split())


# Where OneTee operates. A bounding box is a coarse thing, but for the purpose
# it is exact enough: it exists to stop a name matching a course two thousand
# miles away, and any of these boxes does that. Used only if Nominatim, which
# knows every state, cannot be reached.
STATE_BBOX = {
    "AZ": (31.33, -114.82, 37.00, -109.04),
    "CO": (36.99, -109.06, 41.01, -102.04),
    "FL": (24.40, -87.64, 31.01, -79.97),
    "MD": (37.89, -79.49, 39.73, -74.99),
    "UT": (36.99, -114.05, 42.01, -109.04),
    "VA": (36.54, -83.68, 39.47, -75.24),
}


def state_bbox(session, state: str) -> Optional[tuple]:
    """(south, west, north, east) for a state, from Nominatim or the table."""
    state = state.upper()
    try:
        r = session.get("https://nominatim.openstreetmap.org/search",
                        params={"state": state, "country": "USA", "format": "json",
                                "limit": 1},
                        timeout=30, headers={"User-Agent": UA})
        if r.status_code == 200:
            j = r.json()
            if j and j[0].get("boundingbox"):
                b = [float(x) for x in j[0]["boundingbox"]]
                return (b[0], b[2], b[1], b[3])
        print(f"  nominatim: HTTP {r.status_code} — using the built-in box",
              file=sys.stderr)
    except Exception as exc:
        print(f"  nominatim: {exc.__class__.__name__} — using the built-in box",
              file=sys.stderr)
    return STATE_BBOX.get(state)


def load_state_courses(session, state: str) -> List[Dict]:
    """
    Fetch every named `leisure=golf_course` in the state in ONE query.

    Two earlier shapes of this failed, and both failures are worth keeping in
    mind. Asking Overpass once per course took fifty seconds each against a
    free shared service — 68 courses did not finish inside the job. Asking once
    for the whole state by ADMINISTRATIVE AREA returned 504 from every mirror:
    building the Colorado area is expensive, and the public instances will not
    wear it.

    A bounding box is cheap, and for this purpose it is exact enough. The state
    constraint exists to stop a name matching a course two thousand miles away
    — this directory has already shipped a Mountain Meadows in Pomona,
    California in place of the one in Red Feather Lakes — and a box does that
    perfectly well.
    """
    state = state.upper()
    if state in _state_courses:
        return _state_courses[state]

    box = state_bbox(session, state)
    if not box:
        print(f"  no bounding box for {state} — geo sources will be skipped",
              file=sys.stderr)
        _state_courses[state] = []
        return []
    bbox = ",".join(f"{v:.4f}" for v in box)

    q = ("[out:json][timeout:120];"
         "("
         f'way["leisure"="golf_course"]["name"]({bbox});'
         f'relation["leisure"="golf_course"]["name"]({bbox});'
         ");"
         "out center tags;")

    for url in OVERPASS_MIRRORS:
        try:
            r = session.post(url, data={"data": q}, timeout=150,
                             headers={"User-Agent": UA})
        except Exception as exc:
            print(f"  overpass {url.split('/')[2]}: {exc.__class__.__name__}",
                  file=sys.stderr)
            continue
        if r.status_code != 200:
            # Say so. A swallowed 429 or 504 is what made an earlier run look
            # like "there is simply nothing out there".
            print(f"  overpass {url.split('/')[2]}: HTTP {r.status_code}",
                  file=sys.stderr)
            continue
        try:
            els = r.json().get("elements") or []
        except ValueError:
            print(f"  overpass {url.split('/')[2]}: bad JSON", file=sys.stderr)
            continue

        out = []
        for el in els:
            c = el.get("center") or ({"lat": el.get("lat"), "lon": el.get("lon")}
                                     if el.get("lat") is not None else None)
            nm = (el.get("tags") or {}).get("name")
            if c and nm and c.get("lat") is not None:
                out.append({"name": nm, "norm": _norm(nm),
                            "lat": float(c["lat"]), "lng": float(c["lon"])})
        print(f"  overpass: {len(out)} named golf courses in the {state} box",
              flush=True)
        _state_courses[state] = out
        return out

    print(f"  overpass: every mirror failed for {state} — no coordinates "
          f"available, so the geo sources will be skipped", file=sys.stderr)
    _state_courses[state] = []
    return []


def match_course(courses: List[Dict], name: str) -> Optional[tuple]:
    """
    Match one of our courses to an OSM one, by name, within the state.

    Constraining to the state first is what makes this safe: this directory has
    already shipped a Mountain Meadows in Pomona, California in place of the one
    in Red Feather Lakes. Inside one state a loose name match is fine; across
    the country it is a liability.
    """
    want = _norm(name)
    if not want:
        return None
    best, best_score = None, 0.0
    wset = set(want.split())
    for c in courses:
        if not c["norm"]:
            continue
        if c["norm"] == want:
            return (c["lat"], c["lng"])
        cset = set(c["norm"].split())
        overlap = wset & cset
        if not overlap:
            continue
        score = len(overlap) / max(len(wset), len(cset))
        if score > best_score:
            best, best_score = c, score
    # Two words in common out of three is a match; one word out of four is a
    # coincidence. 0.6 keeps "Lake Estes" and rejects "Lake Valley".
    if best and best_score >= 0.6:
        return (best["lat"], best["lng"])
    return None


def from_wikimedia(session, course, radius_m=1200, limit=12) -> List[Dict]:
    """Commons files geotagged near the course. No key required."""
    lat, lng = course["lat"], course["lng"]
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "geosearch",
        "ggscoord": f"{lat}|{lng}", "ggsradius": str(radius_m),
        "ggslimit": str(limit), "ggsnamespace": "6",
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": "1400",
    }
    try:
        r = session.get("https://commons.wikimedia.org/w/api.php",
                        params=params, timeout=TIMEOUT,
                        headers={"User-Agent": UA})
        if r.status_code != 200:
            return []
        pages = (r.json().get("query") or {}).get("pages") or []
    except Exception:
        return []

    out = []
    for p in pages:
        for ii in (p.get("imageinfo") or []):
            url = ii.get("thumburl") or ii.get("url")
            if not url:
                continue
            meta = ii.get("extmetadata") or {}
            lic = (meta.get("LicenseShortName") or {}).get("value") or "see Commons"
            author = (meta.get("Artist") or {}).get("value") or ""
            out.append({
                "url": url, "w": ii.get("thumbwidth") or ii.get("width"),
                "h": ii.get("thumbheight") or ii.get("height"),
                "source": "wikimedia", "licence": lic,
                "credit": _strip_tags(author)[:80],
                "page": ii.get("descriptionurl") or "",
            })
    return out


def from_flickr(session, course, key, radius_km=1.5, limit=12) -> List[Dict]:
    """Geotagged Flickr photos under a licence that allows commercial use."""
    params = {
        "method": "flickr.photos.search", "api_key": key, "format": "json",
        "nojsoncallback": "1", "content_type": "1", "media": "photos",
        "lat": course["lat"], "lon": course["lng"], "radius": radius_km,
        "radius_units": "km", "license": ",".join(sorted(FLICKR_LICENCES)),
        "sort": "relevance", "per_page": limit,
        "extras": "url_l,url_c,license,owner_name,o_dims",
        "text": "golf",
    }
    try:
        r = session.get("https://api.flickr.com/services/rest/", params=params,
                        timeout=TIMEOUT, headers={"User-Agent": UA})
        if r.status_code != 200:
            return []
        photos = ((r.json().get("photos") or {}).get("photo")) or []
    except Exception:
        return []

    out = []
    for p in photos:
        url = p.get("url_l") or p.get("url_c")
        if not url:
            continue
        out.append({
            "url": url, "w": _int(p.get("width_l") or p.get("width_c")),
            "h": _int(p.get("height_l") or p.get("height_c")),
            "source": "flickr",
            "licence": FLICKR_LICENCES.get(str(p.get("license")), "unknown"),
            "credit": (p.get("ownername") or "")[:80],
            "page": f"https://www.flickr.com/photos/{p.get('owner')}/{p.get('id')}",
        })
    return out


def from_brave(session, course, key, limit=12) -> List[Dict]:
    """
    Brave image search, by name and town.

    This is the one source here with no licence attached. Brave's terms say the
    API grants no rights to the underlying content, so every result is marked
    `unknown` and the picker shows that in orange. Treat a hit as a lead to
    check, not as a picture you may publish.
    """
    q = f"{course['name']} golf course {course.get('city','')} {course.get('state','')}"
    try:
        r = session.get("https://api.search.brave.com/res/v1/images/search",
                        params={"q": q, "count": limit, "safesearch": "strict"},
                        timeout=TIMEOUT,
                        headers={"User-Agent": UA, "Accept": "application/json",
                                 "X-Subscription-Token": key})
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
    except Exception:
        return []

    out = []
    for it in results:
        props = it.get("properties") or {}
        url = props.get("url") or (it.get("thumbnail") or {}).get("src")
        if not url:
            continue
        out.append({
            "url": url, "w": _int(props.get("width")), "h": _int(props.get("height")),
            "source": "brave", "licence": "unknown — check the publisher",
            "credit": (it.get("source") or "")[:80],
            "page": it.get("url") or "",
        })
    return out


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()


# --------------------------------------------------------------------------

def rank(c: Dict) -> float:
    """Landscape and large, same shape rule the website crawl uses."""
    w, h = c.get("w") or 0, c.get("h") or 0
    if not w or not h:
        return 0.0
    if w < 500 or h < 300:
        return -1000.0
    ratio = w / float(h)
    score = 0.0
    if 1.3 <= ratio <= 2.8:
        score += 45
    elif ratio < 0.95:
        score -= 60
    score += min(25.0, (w * h) / 100_000.0)
    # A licence you can actually rely on is worth more than a bigger picture.
    if c.get("source") in ("wikimedia", "flickr"):
        score += 20
    if c.get("source") == "usgs":
        score -= 10          # correct, free, and clearly not a ground photo
    return score


def main() -> int:
    ap = argparse.ArgumentParser(description="Photos for courses with no usable website.")
    ap.add_argument("--state", default="CO")
    ap.add_argument("--only-file", default=None, help="JSON list of venue_ids")
    ap.add_argument("--full", default="data/course_photos_full.json")
    ap.add_argument("--directory", default="directory.json")
    ap.add_argument("--sources", default="wikimedia,flickr,brave,usgs")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between calls")
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    flickr_key = os.environ.get("FLICKR_API_KEY", "").strip()
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if "flickr" in sources and not flickr_key:
        print("no FLICKR_API_KEY — skipping flickr", file=sys.stderr)
        sources.remove("flickr")
    if "brave" in sources and not brave_key:
        print("no BRAVE_API_KEY — skipping brave", file=sys.stderr)
        sources.remove("brave")
    if not sources:
        sys.exit("no usable sources")

    with open(args.full, encoding="utf-8") as fh:
        rows = json.load(fh)
    with open(args.directory, encoding="utf-8") as fh:
        directory = json.load(fh).get("courses") or []
    geo = {c["venue_id"]: c for c in directory}

    wanted = None
    if args.only_file:
        with open(args.only_file, encoding="utf-8") as fh:
            wanted = set(json.load(fh))

    targets = []
    for r in rows:
        vid = r.get("venue_id")
        if wanted is not None and vid not in wanted:
            continue
        if wanted is None:
            if r.get("image"):
                continue
            if args.state and (geo.get(vid, {}).get("state") or "") != args.state.upper():
                continue
        targets.append(r)

    if not targets:
        sys.exit("nothing to look up")

    print(f"looking up {len(targets)} courses via {', '.join(sources)}", flush=True)
    session = requests.Session()
    # One request for the whole state, before the per-course loop starts.
    osm_courses = load_state_courses(session, args.state) if any(
        x in sources for x in ("wikimedia", "flickr", "usgs")) else []
    added = 0
    no_coords = []

    for i, r in enumerate(targets, 1):
        vid = r["venue_id"]
        d = geo.get(vid) or {}
        lat, lng = d.get("lat"), d.get("lng")
        geo_from = "directory"
        if not (lat and lng):
            found_geo = match_course(osm_courses, r.get("name") or vid)
            if found_geo:
                lat, lng = found_geo
                geo_from = "osm"
        course = {"name": r.get("name") or vid, "city": d.get("city", ""),
                  "state": d.get("state", "") or args.state, "lat": lat, "lng": lng}
        if lat and lng:
            r["geo"] = {"lat": lat, "lng": lng, "from": geo_from}

        found: List[Dict] = []
        if lat and lng:
            if "wikimedia" in sources:
                found += from_wikimedia(session, course)
                time.sleep(args.sleep)
            if "flickr" in sources:
                found += from_flickr(session, course, flickr_key)
                time.sleep(args.sleep)
        else:
            # Without coordinates the geo sources cannot be asked at all, and a
            # name search is exactly the collision risk this script exists to
            # avoid. Say so rather than guessing.
            no_coords.append(course["name"])

        if "brave" in sources:
            found += from_brave(session, course, brave_key)
            time.sleep(args.sleep)

        seen = {c["url"] for c in (r.get("alts") or [])}
        if r.get("image"):
            seen.add(r["image"])
        fresh = [c for c in found if c["url"] not in seen and rank(c) > -500]
        fresh.sort(key=rank, reverse=True)

        if fresh:
            r.setdefault("alts", [])
            r["alts"] = fresh[:8] + r["alts"]
            r["note"] = (r.get("note") or "") + " · supplemented"
            added += 1

        # The aerial is a floor, not a candidate to beat the others: it is only
        # offered when nothing else turned up, and always sits last.
        if "usgs" in sources and lat and lng and not fresh and not r.get("image"):
            r.setdefault("alts", []).append({
                "url": usgs_url(float(lat), float(lng)),
                "w": 1000, "h": 640, "source": "usgs",
                "licence": "public domain (USGS)", "credit": "USGS The National Map",
                "page": "https://www.usgs.gov/programs/national-geospatial-program/national-map",
            })
            r["note"] = (r.get("note") or "") + " · aerial only"
            added += 1

        if i % 10 == 0 or i == len(targets):
            print(f"  {i}/{len(targets)}  supplemented {added}", flush=True)

    with open(args.full, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, sort_keys=True)

    print(f"\nDone. {added}/{len(targets)} courses gained candidates.")
    if no_coords:
        print(f"{len(no_coords)} had no coordinates, so only name-based sources "
              f"could be asked: {', '.join(no_coords[:8])}"
              + (" …" if len(no_coords) > 8 else ""))
    print(f"  rewrote {args.full} — nothing was chosen, run the picker next")
    return 0


if __name__ == "__main__":
    sys.exit(main())
