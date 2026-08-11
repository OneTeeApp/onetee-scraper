#!/usr/bin/env python3
"""
Find each course's illustrated course map.

WHY THIS EXISTS
The Tee Times card can open to show what you are booking. Brian wants the
picture in that panel to be the course's illustrated layout — the stylised
hole-by-hole routing artwork — rather than a photograph.

Those maps exist and look great, but there is no universal path to them. Two
real examples measured on 2026-08-10:

  * Antler Creek  -> home page has a "Course Layout" link at /-course-layout,
    which carries `course map.jpg`: a full 18-hole illustrated map, landscape,
    numbered holes. Served from cdn.cybergolf.com, a shared website-vendor CDN,
    so the same shape recurs across that vendor's whole client base.

  * CommonGround  -> "Course Tour" at /golf-course/hole-descriptions holds
    eighteen separate per-hole illustrations (CG-Hole-1.jpg, portrait) and NO
    single overview map at all.

So: follow the layout-ish link, gather candidate images, score them, keep the
best one, and be honest about which courses came up empty. This is a one-off
(or occasional) crawl that produces a table to review by eye — NOT something
that runs when a golfer opens a card.

OUTPUT
  data/course_maps.json         venue_id -> {image, page, score, w, h, kind}
  data/course_maps_review.html  contact sheet: every hit rendered, so 240
                                courses can be eyeballed in one scroll

USAGE
  python scripts/find_course_maps.py --state CO
  python scripts/find_course_maps.py --state CO --only commonground-golf-course
  python scripts/find_course_maps.py --state CO --limit 25 --workers 8

MANNERS
Identifies itself honestly, respects robots.txt, one-second-ish pacing per
host, hard timeouts, and never downloads more of an image than it needs to read
the width and height out of the header.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("This script needs `requests` (pip install requests).")


DIRECTORY_URL = "https://onetee-api.damp-snow-8025.workers.dev/api/directory"

UA = "OneTeeBot/1.0 (+https://www.oneteeapp.com)"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

PAGE_TIMEOUT = 12
IMAGE_TIMEOUT = 8
MAX_HTML_BYTES = 600_000
IMAGE_HEADER_BYTES = 32_768

# Links worth following from the home page, best guesses first.
LINK_HINTS = re.compile(
    r"course[-_ ]?(map|layout|tour)|layout|scorecard|hole[-_ ]?by[-_ ]?hole"
    r"|the[-_ ]?course|our[-_ ]?course|golf[-_ ]?course|holes?\b",
    re.I,
)

# An image whose name says "this is the whole course".
STRONG_MAP = re.compile(r"course[-_ %]*map|course[-_ %]*layout|routing|overview[-_ %]*map", re.I)
WEAK_MAP = re.compile(r"\blayout\b|\bmap\b|\baerial\b|yardage", re.I)
# A single hole rather than the course.
HOLE_ONLY = re.compile(r"hole[-_ %]*\d{1,2}\b|\bhole\d{1,2}\b|^\d{1,2}\.(jpg|png)", re.I)
# Chrome, furniture, and advertising.
JUNK = re.compile(
    r"logo|icon|favicon|sprite|banner|header|footer|nav[-_]|button|btn[-_]"
    r"|sponsor|advert|\bads?[-_]|placeholder|spacer|pixel|avatar|profile"
    r"|facebook|twitter|instagram|youtube|linkedin|weather|arrow|bullet",
    re.I,
)

IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.I)
IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
ATTR = re.compile(r"\b(src|alt|title|width|height|data-src|data-lazy-src)=[\"']([^\"']*)[\"']", re.I)
A_HREF = re.compile(r"<a\b[^>]*?\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TAGS = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# image dimensions, read from the first few KB rather than the whole file
# --------------------------------------------------------------------------

def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Width/height straight out of the file header. No Pillow dependency."""
    if len(data) < 16:
        return None

    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        try:
            if chunk == b"VP8X" and len(data) >= 30:
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
            if chunk == b"VP8L" and len(data) >= 25:
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                bits = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        except Exception:
            return None

    if data[:2] == b"\xff\xd8":  # JPEG: walk the segment chain to SOFn
        i = 2
        end = len(data)
        while i + 9 < end:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            try:
                seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            except struct.error:
                return None
            if seglen < 2:
                return None
            i += 2 + seglen
    return None


# --------------------------------------------------------------------------
# polite fetching
# --------------------------------------------------------------------------

_robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}


def robots_allow(session: requests.Session, url: str) -> bool:
    """Honour robots.txt. A missing or unreadable file is treated as allow."""
    parts = urllib.parse.urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(root + "/robots.txt", timeout=8, headers=HEADERS)
            if r.status_code == 200 and len(r.text) < 500_000:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None
        _robots[root] = rp
    rp = _robots[root]
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def get_html(session: requests.Session, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (final_url, html). Caps the read so one huge page can't stall us."""
    if not robots_allow(session, url):
        return None, None
    try:
        r = session.get(url, timeout=PAGE_TIMEOUT, headers=HEADERS,
                        allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None, None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype:
            return None, None
        chunks, total = [], 0
        for chunk in r.iter_content(16_384):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_HTML_BYTES:
                break
        r.close()
        html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
        return r.url, html
    except Exception:
        return None, None


def get_image_header(session: requests.Session, url: str) -> Optional[bytes]:
    """First slice of an image, enough to read its dimensions."""
    try:
        r = session.get(url, timeout=IMAGE_TIMEOUT, headers={"User-Agent": UA},
                        stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "image" not in ctype:
            return None
        data = r.raw.read(IMAGE_HEADER_BYTES, decode_content=True)
        r.close()
        return data
    except Exception:
        return None


# --------------------------------------------------------------------------
# candidate discovery and scoring
# --------------------------------------------------------------------------

def candidate_pages(base_url: str, html: str, limit: int = 4) -> List[str]:
    """Layout-ish links from the home page, best first, deduped, same-site."""
    host = urllib.parse.urlsplit(base_url).netloc.lower()
    scored: List[Tuple[int, str]] = []
    seen = set()

    for href, inner in A_HREF.findall(html):
        text = TAGS.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        blob = f"{href} {text}"
        if not LINK_HINTS.search(blob):
            continue

        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        # Stay on the course's own site; vendor CDNs are fine for images but
        # we should not wander off into booking engines or social media.
        if parts.netloc.lower() != host:
            continue
        clean = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(clean)

        score = 0
        if re.search(r"course[-_ ]?map|course[-_ ]?layout", blob, re.I):
            score += 60
        if re.search(r"course[-_ ]?tour|hole[-_ ]?by[-_ ]?hole", blob, re.I):
            score += 40
        if re.search(r"\blayout\b", blob, re.I):
            score += 30
        if re.search(r"scorecard|yardage", blob, re.I):
            score += 15
        if re.search(r"the[-_ ]?course|our[-_ ]?course|golf[-_ ]?course|holes?\b", blob, re.I):
            score += 10
        scored.append((score, clean))

    scored.sort(key=lambda t: -t[0])
    return [u for _, u in scored[:limit]]


def collect_images(page_url: str, html: str) -> List[Dict]:
    """Every <img> on a page, with whatever metadata the markup gives us."""
    out: List[Dict] = []
    for tag in IMG_TAG.findall(html):
        attrs = {k.lower(): v for k, v in ATTR.findall(tag)}
        src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        absolute = urllib.parse.urljoin(page_url, src)
        if not absolute.lower().startswith("http"):
            continue
        out.append({
            "url": absolute,
            "alt": (attrs.get("alt") or "") + " " + (attrs.get("title") or ""),
            "w_attr": _int(attrs.get("width")),
            "h_attr": _int(attrs.get("height")),
            "page": page_url,
        })
    return out


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip().rstrip("px"))
    except Exception:
        return None


def score_candidate(cand: Dict, page_matched_layout: bool) -> int:
    """
    Rank an image by how likely it is to be THE course map.

    Filename and alt text carry most of the signal: a file literally called
    "course map.jpg" is the thing we want. Shape is the tiebreaker — an
    overview map is landscape, whereas per-hole diagrams are usually portrait.
    """
    name = urllib.parse.unquote(cand["url"].rsplit("/", 1)[-1])
    blob = f"{name} {cand['alt']}"

    if JUNK.search(blob):
        return -1000

    score = 0
    if STRONG_MAP.search(blob):
        score += 120
    elif WEAK_MAP.search(blob):
        score += 45

    # A single hole is a weak consolation prize, not the goal.
    if HOLE_ONLY.search(blob):
        score -= 60

    if page_matched_layout:
        score += 25

    w = cand.get("w") or cand.get("w_attr")
    h = cand.get("h") or cand.get("h_attr")
    if w and h:
        if w < 300 or h < 200:
            return -1000
        ratio = w / float(h)
        if 1.15 <= ratio <= 2.4:
            score += 35          # landscape: overview shape
        elif ratio < 0.9:
            score -= 35          # portrait: almost always a single hole
        area = w * h
        if area >= 1_200_000:
            score += 20
        elif area >= 500_000:
            score += 12
        elif area >= 200_000:
            score += 5
    return score


def classify(cand: Dict) -> str:
    name = urllib.parse.unquote(cand["url"].rsplit("/", 1)[-1])
    blob = f"{name} {cand['alt']}"
    if STRONG_MAP.search(blob):
        return "overview"
    if HOLE_ONLY.search(blob):
        return "hole"
    return "unknown"


# --------------------------------------------------------------------------
# per-course worker
# --------------------------------------------------------------------------

def find_map_for(course: Dict, verbose: bool = False) -> Dict:
    vid = course.get("venue_id") or ""
    site = (course.get("website") or "").strip()
    result = {
        "venue_id": vid,
        "name": course.get("name"),
        "city": course.get("city"),
        "website": site,
        "image": None,
        "page": None,
        "score": None,
        "w": None,
        "h": None,
        "kind": None,
        "note": "",
    }
    if not site.lower().startswith("http"):
        result["note"] = "no website"
        return result

    session = requests.Session()
    try:
        home_url, home_html = get_html(session, site)
        if not home_html:
            result["note"] = "home page unreachable"
            return result

        pages = candidate_pages(home_url, home_html)
        # The home page itself occasionally carries the map.
        scan: List[Tuple[str, str, bool]] = [(home_url, home_html, False)]
        for p in pages:
            time.sleep(0.4)
            final, html = get_html(session, p)
            if html:
                scan.append((final, html, True))

        candidates: List[Dict] = []
        for page_url, html, matched in scan:
            for cand in collect_images(page_url, html):
                cand["page_matched"] = matched
                cand["prescore"] = score_candidate(cand, matched)
                candidates.append(cand)

        # Cheap pass first; only measure the plausible ones, so we are not
        # downloading a hundred images per course to check their shape.
        candidates = [c for c in candidates if c["prescore"] > -500]
        candidates.sort(key=lambda c: -c["prescore"])

        # The same image usually appears on several pages (headers, galleries).
        # Deduping first means the shortlist holds six DIFFERENT pictures rather
        # than six copies of one.
        shortlist, seen_urls = [], set()
        for cand in candidates:
            if cand["url"] in seen_urls:
                continue
            seen_urls.add(cand["url"])
            shortlist.append(cand)
            if len(shortlist) >= 6:
                break

        best = None
        for cand in shortlist:
            data = get_image_header(session, cand["url"])
            size = image_size(data) if data else None
            if size:
                cand["w"], cand["h"] = size
            cand["score"] = score_candidate(cand, cand["page_matched"])
            if cand["score"] <= 0:
                continue
            if best is None or cand["score"] > best["score"]:
                best = cand

        if not best:
            result["note"] = "no map-like image found"
            return result

        result.update({
            "image": best["url"],
            "page": best["page"],
            "score": best["score"],
            "w": best.get("w"),
            "h": best.get("h"),
            "kind": classify(best),
        })
        return result
    except Exception as exc:  # never let one bad site kill the run
        result["note"] = f"error: {exc.__class__.__name__}"
        return result
    finally:
        session.close()


# --------------------------------------------------------------------------
# review sheet
# --------------------------------------------------------------------------

def write_review(rows: List[Dict], path: str) -> None:
    hits = [r for r in rows if r.get("image")]
    misses = [r for r in rows if not r.get("image")]
    hits.sort(key=lambda r: (r.get("kind") != "overview", -(r.get("score") or 0)))

    cards = []
    for r in hits:
        cards.append(
            '<figure class="c">'
            f'<img loading="lazy" src="{escape(r["image"])}" alt="">'
            f'<figcaption><b>{escape(r["name"] or "")}</b><br>'
            f'<span class="m">{escape(r.get("city") or "")} · '
            f'{escape(str(r.get("kind")))} · score {r.get("score")} · '
            f'{r.get("w") or "?"}×{r.get("h") or "?"}</span><br>'
            f'<a href="{escape(r["page"] or "")}" target="_blank">source page</a> · '
            f'<a href="{escape(r["image"])}" target="_blank">image</a><br>'
            f'<code>{escape(r["venue_id"])}</code></figcaption></figure>'
        )

    miss_rows = "".join(
        f'<tr><td>{escape(m["name"] or "")}</td><td>{escape(m.get("city") or "")}</td>'
        f'<td><a href="{escape(m.get("website") or "")}" target="_blank">'
        f'{escape(m.get("website") or "")}</a></td>'
        f'<td>{escape(m.get("note") or "")}</td></tr>'
        for m in misses
    )

    html = f"""<!doctype html>
<meta charset="utf-8">
<title>OneTee course maps — review</title>
<style>
 body {{ font:15px/1.5 system-ui,sans-serif; margin:24px; background:#f6f5f2; color:#111; }}
 h1 {{ margin:0 0 4px; }} .sum {{ color:#555; margin-bottom:20px; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:18px; }}
 .c {{ margin:0; background:#fff; border:1px solid #ddd; border-radius:10px; padding:10px; }}
 .c img {{ width:100%; height:180px; object-fit:contain; background:#eee; border-radius:6px; }}
 figcaption {{ font-size:12px; margin-top:8px; }} .m {{ color:#666; }}
 code {{ font-size:11px; color:#888; }}
 table {{ border-collapse:collapse; width:100%; margin-top:12px; background:#fff; }}
 td,th {{ border:1px solid #ddd; padding:6px 8px; font-size:13px; text-align:left; }}
</style>
<h1>Course maps — review</h1>
<p class="sum"><b>{len(hits)}</b> found of <b>{len(rows)}</b> courses
 ({len([r for r in hits if r.get('kind') == 'overview'])} look like full overview maps).
 Anything wrong here is a candidate for the override list.</p>
<div class="grid">{''.join(cards)}</div>
<h2>No map found ({len(misses)})</h2>
<table><tr><th>Course</th><th>City</th><th>Website</th><th>Why</th></tr>{miss_rows}</table>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# --------------------------------------------------------------------------

def normalise(row: Dict) -> Dict:
    """
    The directory is served from a generated bundle, so field names are stable
    but not guaranteed to match what this script wants. Accept the obvious
    aliases rather than failing on a rename.
    """
    def first(*keys):
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    return {
        "venue_id": first("venue_id", "venueId", "id", "vid"),
        "name": first("name", "course_name", "courseName", "title"),
        "city": first("city", "town"),
        "state": first("state", "st", "state_abbr").upper(),
        "website": first("website", "site", "url", "web"),
        "_raw": row,
    }


def load_courses(state: str, only: Optional[str]) -> List[Dict]:
    r = requests.get(DIRECTORY_URL, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    payload = r.json()

    if isinstance(payload, list):
        raw = payload
    else:
        raw = None
        for key in ("list", "courses", "venues", "items", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                raw = v
                break
        if raw is None:
            raise SystemExit(
                "Could not find the course list in /api/directory. Top-level keys: "
                + ", ".join(sorted(payload.keys()))
            )

    rows = [normalise(c) for c in raw if isinstance(c, dict)]
    if not any(r_["venue_id"] for r_ in rows):
        sample = sorted(raw[0].keys()) if raw and isinstance(raw[0], dict) else []
        raise SystemExit(
            "No venue_id on any directory row — field names must have changed. "
            "First row's keys: " + ", ".join(sample)
        )

    if state:
        rows = [c for c in rows if c["state"] == state.upper()]
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        rows = [c for c in rows if c["venue_id"] in wanted]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Find illustrated course maps for OneTee courses.")
    ap.add_argument("--state", default="CO", help="two-letter state, blank for all (default CO)")
    ap.add_argument("--only", default=None, help="comma-separated venue_ids, for spot checks")
    ap.add_argument("--limit", type=int, default=0, help="stop after N courses (0 = all)")
    ap.add_argument("--workers", type=int, default=6, help="parallel courses (default 6)")
    ap.add_argument("--out", default="data/course_maps.json")
    ap.add_argument("--review", default="data/course_maps_review.html")
    args = ap.parse_args()

    courses = load_courses(args.state, args.only)
    if args.limit:
        courses = courses[:args.limit]
    if not courses:
        print("No courses matched.", file=sys.stderr)
        return 1

    print(f"Crawling {len(courses)} courses with {args.workers} workers…", flush=True)
    rows: List[Dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(find_map_for, c): c for c in courses}
        for fut in as_completed(futures):
            rows.append(fut.result())
            done += 1
            if done % 10 == 0 or done == len(courses):
                found = len([r for r in rows if r.get("image")])
                print(f"  {done}/{len(courses)}  found {found}", flush=True)

    rows.sort(key=lambda r: (r.get("name") or "").lower())

    for path in (args.out, args.review):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    table = {
        r["venue_id"]: {
            "image": r["image"], "page": r["page"], "kind": r["kind"],
            "w": r["w"], "h": r["h"], "score": r["score"],
        }
        for r in rows if r.get("image")
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2, sort_keys=True)
    write_review(rows, args.review)

    hits = len(table)
    overview = len([r for r in rows if r.get("kind") == "overview"])
    print(f"\nDone. {hits}/{len(rows)} courses have a candidate map "
          f"({overview} look like full overview maps).")
    print(f"  table:  {args.out}")
    print(f"  review: {args.review}   <- open this and eyeball it before shipping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
