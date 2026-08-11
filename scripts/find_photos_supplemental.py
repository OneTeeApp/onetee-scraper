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


OVERPASS = "https://overpass-api.de/api/interpreter"
_geo_cache: Dict[str, Optional[tuple]] = {}


def geocode_overpass(session, name: str, state: str) -> Optional[tuple]:
    """
    Find the course's own polygon in OpenStreetMap and return its centre.

    Neither `directory.json` nor the live `/api/directory` carries coordinates —
    the geotagging work put them in D1's `venue_geo` and joined them into
    `/api/tee-times` only, and the merge into the directory is still an open
    item. Without a lat/lng the geo sources cannot be asked at all, which is
    why the first supplemental run added nothing.

    Asking OSM for `leisure=golf_course` **inside the state boundary** solves
    two problems at once. It returns the centre of the actual course rather
    than the middle of the nearest town, which is what a USGS aerial needs to
    be framed on the right fairways. And constraining to the state makes the
    collision that has bitten this directory before impossible: there is a
    Mountain Meadows in Pomona, California, and it is not in Colorado.
    """
    key = f"{state}|{name}".lower()
    if key in _geo_cache:
        return _geo_cache[key]

    # Strip the words that appear on every course and carry no signal, so the
    # regex is matching the distinctive part of the name.
    stem = re.sub(r"\b(golf|course|club|country|links|municipal|the|at|and|&)\b",
                  " ", name, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9 ]", " ", stem).strip()
    stem = " ".join(stem.split()[:3])
    if len(stem) < 3:
        _geo_cache[key] = None
        return None

    q = (
        "[out:json][timeout:25];"
        f'area["ISO3166-2"="US-{state.upper()}"][admin_level=4]->.a;'
        "("
        f'way["leisure"="golf_course"]["name"~"{stem}",i](area.a);'
        f'relation["leisure"="golf_course"]["name"~"{stem}",i](area.a);'
        f'node["leisure"="golf_course"]["name"~"{stem}",i](area.a);'
        ");"
        "out center 3;"
    )
    try:
        r = session.post(OVERPASS, data={"data": q}, timeout=60,
                         headers={"User-Agent": UA})
        if r.status_code != 200:
            _geo_cache[key] = None
            return None
        els = r.json().get("elements") or []
    except Exception:
        _geo_cache[key] = None
        return None

    for el in els:
        centre = el.get("center") or ({"lat": el.get("lat"), "lon": el.get("lon")}
                                      if el.get("lat") else None)
        if centre and centre.get("lat") and centre.get("lon"):
            out = (float(centre["lat"]), float(centre["lon"]))
            _geo_cache[key] = out
            return out
    _geo_cache[key] = None
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
    added = 0
    no_coords = []

    for i, r in enumerate(targets, 1):
        vid = r["venue_id"]
        d = geo.get(vid) or {}
        lat, lng = d.get("lat"), d.get("lng")
        geo_from = "directory"
        if not (lat and lng):
            found_geo = geocode_overpass(session, r.get("name") or vid,
                                         d.get("state") or args.state)
            if found_geo:
                lat, lng = found_geo
                geo_from = "osm"
                time.sleep(max(args.sleep, 1.0))   # Overpass is a shared service
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
