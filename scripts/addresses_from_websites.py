"""Recover a course's real location from its own website.

Where this fits: `regeocode_precise.py` resolves a venue by matching its name
against OSM's golf-course geometry, and falls back to the venue's ZIP centroid.
That leaves a hard residue — measured 2026-09-01, Florida still had 179 venues
sharing a coordinate with another venue, i.e. still sitting on a city centroid.

Filling in ZIP codes does not fix that on its own, and it is worth being clear
why: a ZIP centroid is still a centroid, and you cannot look up a course's ZIP
without already knowing where the course is. The circle only breaks by going to
a source that states the address outright — and 158 of those 179 Florida venues
publish one on their own website.

So this reads each venue's site, extracts a postal address, and geocodes THAT.
Three extraction strategies, best first:

  1. schema.org JSON-LD (`PostalAddress`) — the site telling us structurally.
  2. microdata `itemprop="streetAddress"` / `postalCode`.
  3. a street-address regex over the visible text, anchored on the venue's own
     state so a franchise footer for another location cannot win.

A hit is only accepted if the geocoded point lands within MAX_KM of the venue's
current anchor, so a stray address in a footer cannot move a course to another
state. The ZIP it finds is reported too — that is what actually fills the empty
`Zip` column in the sheets, which is blank for every state except Colorado
(248/248 there, 0/658 in Florida, 0 everywhere else).

    python -m scripts.addresses_from_websites --states FL --dry-run
    python -m scripts.addresses_from_websites --states FL

Runs on a GitHub runner: the dev sandbox's egress proxy blocks course websites,
Nominatim and the database alike.
"""
from __future__ import annotations

import argparse
import collections
import html
import json
import os
import re
import sys
import time

import requests

from scraper.d1 import D1Rest, HttpBackend, SqliteLocal
from scripts.regeocode_precise import (MAX_KM, NOMINATIM, UA, haversine_km,
                                       plain)

DIRECTORY = "directory.json"
PAGE_TIMEOUT = 20
PAGE_DELAY = 0.8          # polite gap between course websites

STATE_FULL = {
    "AK": "Alaska", "AZ": "Arizona", "CO": "Colorado",
    "DC": "District of Columbia", "FL": "Florida", "MD": "Maryland",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "UT": "Utah",
    "VA": "Virginia", "VT": "Vermont", "WY": "Wyoming",
}

STREET_WORDS = (
    r"Road|Rd|Street|St|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Way|"
    r"Circle|Cir|Court|Ct|Parkway|Pkwy|Trail|Trl|Highway|Hwy|Terrace|Ter|"
    r"Place|Pl|Route|Rte|Loop|Run|Pass|Club|Links"
)


def _text(hpage: str) -> str:
    """Visible-ish text: drop script/style, unescape entities, squash space."""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", hpage)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _walk(node):
    """Every dict in a JSON-LD blob, however deeply nested or listed."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def from_jsonld(hpage: str, state: str) -> dict | None:
    for m in re.finditer(
            r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            hpage):
        try:
            blob = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in _walk(blob):
            if not isinstance(node.get("address"), (dict, list)):
                continue
            for addr in _walk(node["address"]):
                street = addr.get("streetAddress")
                zipc = str(addr.get("postalCode") or "").strip()[:5]
                region = str(addr.get("addressRegion") or "").strip()
                if not street or not re.fullmatch(r"\d{5}", zipc):
                    continue
                # A franchise or booking-widget footer can carry someone else's
                # address; require the state to agree with our own row.
                if region and plain(region) not in (
                        plain(state), plain(STATE_FULL.get(state, ""))):
                    continue
                return {"street": str(street).strip(),
                        "city": str(addr.get("addressLocality") or "").strip(),
                        "zip": zipc, "how": "json-ld"}
    return None


def from_microdata(hpage: str, state: str) -> dict | None:
    def grab(prop):
        m = re.search(rf'(?is)itemprop=["\']{prop}["\'][^>]*>([^<]{{2,80}})<', hpage)
        return html.unescape(m.group(1)).strip() if m else ""
    street, zipc = grab("streetAddress"), grab("postalCode")[:5]
    if street and re.fullmatch(r"\d{5}", zipc):
        return {"street": street, "city": grab("addressLocality"),
                "zip": zipc, "how": "microdata"}
    return None


def _tidy_street(s: str) -> str:
    """Trim a greedy street capture back to the real house number.

    The regex starts at a number, and page text like "Open 7 days. Call
    863-555-1212. 100 Club Drive" gives it an earlier number to latch onto, so
    the whole run gets swallowed. Restart at the LAST number that begins a
    plausible street; "10100 NW 87th Ave" is unaffected because "87th" has no
    space after its digits.
    """
    starts = list(re.finditer(r"\d{1,6}\s+\S", s))
    if len(starts) > 1:
        s = s[starts[-1].start():]
    return s.strip(" ,.")


def from_text(hpage: str, state: str) -> dict | None:
    full = STATE_FULL.get(state, state)
    pat = re.compile(
        rf"(\d{{1,6}}\s+[A-Za-z0-9.'\-]+(?:\s+[A-Za-z0-9.'\-]+){{0,5}}?\s+"
        rf"(?:{STREET_WORDS})\.?)\s*,?\s*"
        rf"([A-Za-z .'\-]{{2,30}}?)\s*,?\s+"
        rf"(?:{re.escape(state)}|{re.escape(full)})\.?\s+(\d{{5}})\b")
    m = pat.search(_text(hpage))
    if m:
        street = _tidy_street(m.group(1))
        if re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", street):
            return None                      # swallowed a phone number
        return {"street": street, "city": m.group(2).strip(),
                "zip": m.group(3), "how": "text-regex"}
    return None


def address_from_site(session: requests.Session, url: str,
                      state: str) -> dict | None:
    try:
        r = session.get(url, timeout=PAGE_TIMEOUT,
                        headers={"User-Agent": UA}, allow_redirects=True)
        r.raise_for_status()
        page = r.text
    except requests.RequestException:
        return None
    for fn in (from_jsonld, from_microdata, from_text):
        hit = fn(page, state)
        if hit:
            return hit
    return None


def geocode_address(session: requests.Session, addr: dict,
                    state: str) -> tuple[float, float] | None:
    q = ", ".join(x for x in (addr["street"], addr.get("city"), state,
                              addr["zip"], "USA") if x)
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


# Sources that already put a venue on the course itself. Never re-resolve one.
PRECISE = {"overpass-osm", "nominatim-bounded", "nominatim-name",
           "nominatim-name-nocity", "website-address"}


def stacked_targets(courses: list[dict], want: set[str],
                    geo: dict[str, dict] | None = None) -> list[dict]:
    """Venues still sharing a coordinate with another venue.

    That is the honest signal for "still on a centroid": a course with its own
    precise point does not collide with anybody. Venues that legitimately share
    a point because they are two courses of one facility are excluded — those
    are already correct, and the map collapses them to a single pin anyway.

    Coordinates come from `venue_geo` when available, NOT from directory.json.
    directory.json is a built artifact that only refreshes when the directory
    workflow runs, so straight after a re-geocode it still shows the old stacked
    points — targeting off it would re-resolve venues that were just fixed and
    could overwrite a good OSM match with a weaker website guess.
    """
    geo = geo or {}
    at = collections.defaultdict(list)
    for c in courses:
        g = geo.get(c.get("venue_id"))
        if g and g.get("lat") is not None:
            c = dict(c, lat=g["lat"], lng=g["lng"])
        if c.get("lat") is None or c.get("lng") is None:
            continue
        if (g or {}).get("source") in PRECISE:
            continue
        at[(round(c["lat"], 5), round(c["lng"], 5))].append(c)
    out = []
    for group in at.values():
        if len(group) < 2:
            continue
        bases = {plain(re.split(r"\s+[-–—]\s+",
                                re.sub(r"\s*\([^()]*\)\s*$", "", c["name"]))[0])
                 for c in group}
        if len(bases) < 2:           # one facility, many courses — fine as is
            continue
        for c in group:
            if not want or c.get("state") in want:
                out.append(c)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Geocode stubborn venues from the address on their website")
    p.add_argument("--local", metavar="SQLITE_FILE")
    p.add_argument("--states", default="")
    p.add_argument("--directory", default=DIRECTORY)
    p.add_argument("--dry-run", action="store_true")
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
    want = {s.strip().upper() for s in a.states.split(",") if s.strip()}
    geo = {r["venue_id"]: r
           for r in db.execute("SELECT venue_id, lat, lng, source FROM venue_geo")}
    targets = [c for c in stacked_targets(courses, want, geo)
               if re.match(r"^https?://", str(c.get("website") or ""))]
    print(f"directory={len(courses)}  still-stacked with a website="
          f"{len(targets)}" + (f"  states={sorted(want)}" if want else ""),
          flush=True)

    session = requests.Session()
    report, zips = [], []
    fixed = nosite = noaddr = nogeo = toofar = 0

    for i, c in enumerate(targets):
        if a.limit and i >= a.limit:
            print(f"hit --limit {a.limit}, stopping", flush=True)
            break
        state = c.get("state", "")
        addr = address_from_site(session, c["website"], state)
        time.sleep(PAGE_DELAY)
        if not addr:
            noaddr += 1
            report.append((state, c["name"], "no address on site", ""))
            continue
        zips.append((c["venue_id"], addr["zip"]))
        pt = geocode_address(session, addr, state)
        time.sleep(1.2)                      # Nominatim policy: <= 1 req/sec
        if not pt:
            nogeo += 1
            report.append((state, c["name"], f"address, not geocodable "
                                             f"({addr['how']})",
                           f"{addr['street']}, {addr['zip']}"))
            continue
        km = haversine_km(c["lat"], c["lng"], pt[0], pt[1])
        if km > MAX_KM:
            toofar += 1
            report.append((state, c["name"], "rejected: too far",
                           f"{addr['street']}, {addr['zip']} — {km:.0f} km"))
            continue
        fixed += 1
        print(f"  website-address {c['name'][:40]:40s} {addr['zip']} "
              f"-> {pt[0]:.5f},{pt[1]:.5f} ({km:.1f} km)", flush=True)
        report.append((state, c["name"], f"fixed ({addr['how']})",
                       f"{addr['street']}, {addr['zip']} → "
                       f"{pt[0]:.5f},{pt[1]:.5f} ({km:.1f} km)"))
        if not a.dry_run:
            db.execute(
                "INSERT INTO venue_geo (venue_id, lat, lng, source) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (venue_id) DO UPDATE SET "
                "lat=EXCLUDED.lat, lng=EXCLUDED.lng, source=EXCLUDED.source",
                [c["venue_id"], pt[0], pt[1], "website-address"])

    verb = "would fix" if a.dry_run else "fixed"
    summary = (f"{verb} {fixed} of {len(targets)} — no-address={noaddr} "
               f"not-geocodable={nogeo} rejected-too-far={toofar} "
               f"ZIPs recovered={len(zips)}")
    print(f"DONE: {summary}", flush=True)
    write_summary(a.dry_run, summary, report, zips)
    return 0


def write_summary(dry_run, summary, report, zips) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    head = "Dry run — nothing written" if dry_run else "Applied to venue_geo"
    lines = [f"## Addresses from course websites: {head}", "", summary, "",
              "### ZIPs recovered (paste into the sheets' `Zip` column)", "",
              "| venue_id | zip |", "|---|---|"]
    lines += [f"| {v} | {z} |" for v, z in zips[:900]]
    lines += ["", "### Every venue attempted", "",
              "| State | Course | Outcome | Detail |", "|---|---|---|---|"]
    for st, name, outcome, detail in report[:900]:
        lines.append(f"| {st} | {name.replace('|', chr(92) + '|')} "
                     f"| {outcome} | {detail} |")
    try:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as e:  # noqa: BLE001
        print(f"  (could not write job summary: {e})", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
