#!/usr/bin/env python3
"""
Find one good photograph of each golf course, on the course's own website.

WHY THIS EXISTS
The Tee Times card opens to show what you are booking, and the picture in that
panel should be a photograph of the course — a fairway, a green, the clubhouse,
some landscape — taken from the course's own site.

The gate Worker already reads each site's `og:image` live. That is the picture a
course chose for link previews, so when it exists it is usually the right one.
But only about a third of Colorado courses publish one, and some of those are a
logo or a wedding party. This crawl does the same job properly and offline: it
reads several pages per site, gathers every image, scores them, and keeps the
best. The result is a static table, reviewed by eye, which beats a live lookup
on coverage, on quality, and on latency.

WHAT COUNTS AS A GOOD PHOTO
Landscape, large, and named or described like scenery. Anything that looks like
site furniture (logos, icons, banners, sponsor strips, social glyphs) is thrown
out outright. So are illustrated course maps and scorecards — this is the photo
crawl, and a routing diagram is not a photograph.

Subjects that reliably contain people — weddings, banquets, outings, leagues,
staff, dining rooms — are penalised hard, because Brian asked for pictures with
nobody in them. Nothing can guarantee an empty frame from a filename, so the
review sheet exists: every pick is rendered as an actual picture, with three
runners-up beside it, and swapping a bad pick is one edit.

OUTPUT
  data/course_photos.json         venue_id -> {image, page, score, w, h}
  data/course_photos_review.html  contact sheet: each pick plus its alternates,
                                  then a table of the courses that came up empty

USAGE
  python scripts/find_course_photos.py --state CO
  python scripts/find_course_photos.py --state CO --limit 25
  python scripts/find_course_photos.py --only commonground-golf-course

MANNERS
Identifies itself honestly, respects robots.txt, paces itself per host, hard
timeouts, and never downloads more of an image than the header it needs to read
the width and height.
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


# The gate is the front door, and the only one that stays up. `onetee-api`'s
# own workers.dev subdomain went dark during the VPS cutover on 2026-08-11 and
# now returns a Cloudflare placeholder 404; the gate still reaches the API over
# its ORIGIN service binding, which is exactly how the live widget gets this
# list. Override with ONETEE_DIRECTORY_URL if the host ever moves again.
DIRECTORY_URLS = [
    os.environ.get("ONETEE_DIRECTORY_URL") or
    "https://onetee-gate.damp-snow-8025.workers.dev/api/directory",
    "https://onetee-api.damp-snow-8025.workers.dev/api/directory",
]

UA = "OneTeeBot/1.0 (+https://www.oneteeapp.com)"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

PAGE_TIMEOUT = 12
IMAGE_TIMEOUT = 8
MAX_HTML_BYTES = 600_000
IMAGE_HEADER_BYTES = 32_768

# Pages worth opening beyond the home page. Galleries and "the course" pages are
# where the scenery lives; rates and events pages are where the people live.
LINK_HINTS = re.compile(
    r"gallery|photo|the[-_ ]?course|our[-_ ]?course|golf[-_ ]?course|course[-_ ]?tour"
    r"|about|clubhouse|hole[-_ ]?by[-_ ]?hole|holes?\b|experience",
    re.I,
)

# Site furniture. Never a photograph of anything.
JUNK = re.compile(
    r"logo|icon|favicon|sprite|banner|header[-_]|footer|nav[-_]|button|btn[-_]"
    r"|sponsor|advert|\bads?[-_]|placeholder|spacer|pixel|blank|loading|swatch"
    r"|avatar|headshot|profile|facebook|twitter|instagram|youtube|linkedin|yelp"
    r"|tripadvisor|weather|arrow|bullet|divider|badge|seal|award|certif"
    r"|paypal|visa|mastercard|giftcard|qr[-_]?code",
    re.I,
)

# Drawings, not photographs. This is the photo crawl — these belong to the idea
# we dropped, and letting one through would put a diagram on the card.
NOT_A_PHOTO = re.compile(
    r"course[-_ %]*map|course[-_ %]*layout|routing|scorecard|yardage[-_ ]?book"
    r"|\bdiagram\b|\billustrat|\bsketch\b|\bdrawing\b|\bchart\b|\bgraphic\b"
    r"|\bflyer\b|\bposter\b|\bmenu\b|\bcoupon\b|\bpricing\b|\brates?\b",
    re.I,
)

# Subjects that nearly always contain people. Brian asked for empty frames.
PEOPLE = re.compile(
    r"wedding|banquet|reception|event|outing|tournament|scramble|league|party"
    r"|dining|restaurant|\bbar\b|grill|patio[-_ ]?dining|lesson|instruct|academy"
    r"|junior|kids|camp|clinic|staff|team|member|group|foursome|golfer|player"
    r"|swing|celebrat|guest|crowd|people|portrait",
    re.I,
)

# Words that say "this is scenery".
SCENIC = re.compile(
    r"\bhole[-_ ]?\d{1,2}\b|fairway|green\b|greens\b|\btee\b|bunker|\brough\b"
    r"|clubhouse|course|aerial|landscape|scenic|scenery|vista|view|panorama"
    r"|sunset|sunrise|mountain|lake|pond|creek|water|links|signature|\bgolf\b",
    re.I,
)

IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
ATTR = re.compile(
    r"\b(src|alt|title|width|height|data-src|data-lazy-src|data-original|srcset|data-srcset)"
    r"=[\"']([^\"']*)[\"']",
    re.I,
)
A_HREF = re.compile(r"<a\b[^>]*?\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TAGS = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# image dimensions, read from the first few KB rather than the whole file
# --------------------------------------------------------------------------

def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Width/height straight out of the file header. No Pillow dependency."""
    if not data or len(data) < 16:
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
                bits = (data[21] | (data[22] << 8) | (data[23] << 16) | (data[24] << 24))
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        except Exception:
            return None

    if data[:2] == b"\xff\xd8":  # JPEG: walk the segment chain to SOFn
        i, end = 2, len(data)
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
        rp: Optional[urllib.robotparser.RobotFileParser] = urllib.robotparser.RobotFileParser()
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


def host_variants(url: str) -> List[str]:
    """
    The same site under the spellings a course actually uses.

    Twenty-one Colorado courses came back "home page unreachable" on the first
    crawl. A single failed GET is not proof a site is down — it is usually the
    other of www/bare refusing, or a host that only answers on http and
    redirects. Try the obvious spellings before believing the site is gone.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    other = host[4:] if host.startswith("www.") else "www." + host
    seen, out = set(), []
    for scheme in ("https", "http"):
        for h in (host, other):
            u = urllib.parse.urlunsplit((scheme, h, parts.path or "/", parts.query, ""))
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def get_html_any(session: requests.Session, url: str) -> Tuple[Optional[str], Optional[str]]:
    """First spelling of the host that answers with HTML."""
    for candidate in host_variants(url):
        final, html = get_html(session, candidate)
        if html:
            return final, html
        time.sleep(0.25)
    return None, None


def get_html(session: requests.Session, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (final_url, html). Caps the read so one huge page can't stall us."""
    if not robots_allow(session, url):
        return None, None
    try:
        r = session.get(url, timeout=PAGE_TIMEOUT, headers=HEADERS,
                        allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None, None
        if "html" not in (r.headers.get("Content-Type") or "").lower():
            return None, None
        chunks, total = [], 0
        for chunk in r.iter_content(16_384):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_HTML_BYTES:
                break
        r.close()
        return r.url, b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        return None, None


def get_image_header(session: requests.Session, url: str) -> Optional[bytes]:
    """First slice of an image, enough to read its dimensions."""
    try:
        r = session.get(url, timeout=IMAGE_TIMEOUT, headers={"User-Agent": UA},
                        stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None
        if "image" not in (r.headers.get("Content-Type") or "").lower():
            return None
        data = r.raw.read(IMAGE_HEADER_BYTES, decode_content=True)
        r.close()
        return data
    except Exception:
        return None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def candidate_pages(base_url: str, html: str, limit: int = 4) -> List[str]:
    """Scenery-ish links from the home page, best first, deduped, same-site."""
    host = urllib.parse.urlsplit(base_url).netloc.lower()
    scored: List[Tuple[int, str]] = []
    seen = set()

    for href, inner in A_HREF.findall(html):
        text = re.sub(r"\s+", " ", TAGS.sub(" ", inner)).strip()
        blob = f"{href} {text}"
        if not LINK_HINTS.search(blob):
            continue

        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        # Stay on the course's own site. A booking engine or a Facebook page is
        # not somewhere we should be wandering.
        if parts.netloc.lower() != host:
            continue
        clean = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        seen.add(clean)

        score = 0
        if re.search(r"gallery|photo", blob, re.I):
            score += 60
        if re.search(r"the[-_ ]?course|our[-_ ]?course|course[-_ ]?tour", blob, re.I):
            score += 45
        if re.search(r"clubhouse", blob, re.I):
            score += 30
        if re.search(r"hole[-_ ]?by[-_ ]?hole|holes?\b", blob, re.I):
            score += 20
        if re.search(r"about|experience", blob, re.I):
            score += 10
        scored.append((score, clean))

    scored.sort(key=lambda t: -t[0])
    return [u for _, u in scored[:limit]]


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip().rstrip("px"))
    except Exception:
        return None


def _widest_from_srcset(value: str) -> Optional[str]:
    """
    A srcset lists the same picture at several sizes. Take the biggest.

    Split on comma-then-whitespace, not on comma. Wix and Squarespace put bare
    commas inside their transform URLs (`w_275,h_155,enc_auto`), and splitting
    on every comma tears a single URL into fragments — which is how Walking
    Stick ended up with a whole srcset string where its image URL should be.

    Descriptors come in two flavours and both have to count: `1200w` is a
    width, `2x` is a pixel density. Ranking on width alone silently returns
    nothing for a density-only srcset, so each entry is scored on whichever it
    carries, and an entry with no descriptor at all still beats returning None.
    """
    best, best_rank = None, float("-inf")
    for part in re.split(r",\s+", value):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        rank = 0.0
        if len(bits) > 1:
            d = bits[1].strip()
            if d.endswith("w"):
                rank = float(_int(d[:-1]) or 0)
            elif d.endswith("x"):
                try:
                    # Densities are left unscaled — they are single digits, so
                    # a width always outranks one. The two are not supposed to
                    # appear in the same srcset anyway; this just decides
                    # sensibly if a hand-written page mixes them.
                    rank = float(d[:-1])
                except ValueError:
                    rank = 0.0
        if rank > best_rank:
            best, best_rank = url, rank
    return best


def collect_images(page_url: str, html: str) -> List[Dict]:
    """Every <img> on a page, plus whatever the <head> nominates as the hero."""
    out: List[Dict] = []
    seen = set()

    def add(src: str, alt: str, w=None, h=None, hero: bool = False):
        # A data: URI is the picture inlined as base64. It is not a URL we can
        # hand a browser from our own page, and one of them reached the picker
        # as a 40KB blob where a link should have been.
        if not src or src.strip().lower().startswith("data:"):
            return
        absolute = urllib.parse.urljoin(page_url, src.strip())
        low = absolute.lower()
        if not low.startswith("http"):
            return
        # SVG is a drawing — a logo, a wordmark, an icon. Never a photograph.
        if low.split("?")[0].endswith(".svg"):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        out.append({"url": absolute, "alt": alt or "", "w_attr": w, "h_attr": h,
                    "page": page_url, "hero": hero})

    # The share image is the course's own answer to "what does this place look
    # like", so it starts with an advantage — but it still has to pass the same
    # filters, because plenty of them are logos.
    for m in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](og:image(?::secure_url)?|twitter:image(?::src)?)["\']'
        r'[^>]*content=["\']([^"\']+)',
        html, re.I,
    ):
        add(m.group(2), "og share image", hero=True)
    for m in re.finditer(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)='
        r'["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
        html, re.I,
    ):
        add(m.group(1), "og share image", hero=True)

    for tag in IMG_TAG.findall(html):
        attrs = {k.lower(): v for k, v in ATTR.findall(tag)}
        src = (attrs.get("src") or attrs.get("data-src")
               or attrs.get("data-lazy-src") or attrs.get("data-original"))
        srcset = attrs.get("srcset") or attrs.get("data-srcset")
        if srcset:
            widest = _widest_from_srcset(srcset)
            if widest:
                src = widest
        add(src or "", (attrs.get("alt") or "") + " " + (attrs.get("title") or ""),
            _int(attrs.get("width")), _int(attrs.get("height")))
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score_candidate(cand: Dict, page_bonus: int) -> int:
    """
    How likely is this to be a good, people-free photograph of the course?

    Name and alt text carry the meaning; measured shape and size decide between
    images that read the same. A hero shot is wide and big; site furniture is
    small; a portrait crop is usually a person.
    """
    name = urllib.parse.unquote(cand["url"].rsplit("/", 1)[-1])
    blob = f"{name} {cand.get('alt') or ''}"

    if JUNK.search(blob):
        return -1000
    if NOT_A_PHOTO.search(blob):
        return -1000

    score = page_bonus

    if cand.get("hero"):
        score += 55
    if SCENIC.search(blob):
        score += 45
    if PEOPLE.search(blob):
        score -= 90

    # Filenames straight off a camera or a CMS carry no meaning either way.
    # They are common and often the best picture on the page, so they are not
    # punished — they simply have to win on shape and size.

    w = cand.get("w") or cand.get("w_attr")
    h = cand.get("h") or cand.get("h_attr")
    if w and h:
        if w < 600 or h < 350:
            return -1000          # too small to fill a card
        ratio = w / float(h)
        if 1.3 <= ratio <= 2.8:
            score += 45           # hero/landscape shape
        elif 1.05 <= ratio < 1.3:
            score += 15
        elif ratio < 0.95:
            score -= 60           # portrait: a person, a menu, or a single hole
        area = w * h
        if area >= 1_500_000:
            score += 25
        elif area >= 700_000:
            score += 15
        elif area >= 350_000:
            score += 5
    return score


# --------------------------------------------------------------------------
# per-course worker
# --------------------------------------------------------------------------

def find_photo_for(course: Dict) -> Dict:
    vid = course.get("venue_id") or ""
    site = (course.get("website") or "").strip()
    result: Dict = {
        "venue_id": vid, "name": course.get("name"), "city": course.get("city"),
        "website": site, "image": None, "page": None, "score": None,
        "w": None, "h": None, "alts": [], "note": "",
    }
    if not site.lower().startswith("http"):
        result["note"] = "no website"
        return result

    session = requests.Session()
    try:
        home_url, home_html = get_html_any(session, site)
        if not home_html:
            result["note"] = "home page unreachable"
            return result

        scan: List[Tuple[str, str, int]] = [(home_url, home_html, 10)]
        pages = candidate_pages(home_url, home_html)
        # Some sites bury the gallery behind a JS menu the crawler cannot read,
        # so the home page links nowhere useful. These paths cost one request
        # each and are where galleries usually live.
        if len(pages) < 3:
            base = home_url
            for guess in ("/gallery", "/photos", "/photo-gallery", "/the-course",
                          "/course", "/golf-course", "/about"):
                u = urllib.parse.urljoin(base, guess)
                if u not in pages:
                    pages.append(u)
            pages = pages[:7]
        for p in pages:
            time.sleep(0.4)
            final, html = get_html(session, p)
            if html:
                bonus = 30 if re.search(r"gallery|photo|course", p, re.I) else 15
                scan.append((final, html, bonus))

        candidates: List[Dict] = []
        seen_urls = set()
        for page_url, html, bonus in scan:
            for cand in collect_images(page_url, html):
                if cand["url"] in seen_urls:
                    continue
                seen_urls.add(cand["url"])
                cand["page_bonus"] = bonus
                cand["prescore"] = score_candidate(cand, bonus)
                candidates.append(cand)

        # Cheap pass first, then measure only the plausible ones — otherwise a
        # gallery page would mean downloading a hundred image headers.
        candidates = [c for c in candidates if c["prescore"] > -500]
        candidates.sort(key=lambda c: -c["prescore"])
        shortlist = candidates[:16]

        measured: List[Dict] = []
        for cand in shortlist:
            size = image_size(get_image_header(session, cand["url"]))
            if size:
                cand["w"], cand["h"] = size
            cand["score"] = score_candidate(cand, cand["page_bonus"])
            if cand["score"] > 0:
                measured.append(cand)

        if not measured:
            result["note"] = "no usable photo found"
            return result

        measured.sort(key=lambda c: -c["score"])
        best = measured[0]
        result.update({
            "image": best["url"], "page": best["page"], "score": best["score"],
            "w": best.get("w"), "h": best.get("h"),
            # Runners-up ride along so a bad pick can be swapped by eye rather
            # than by re-running the whole crawl.
            "alts": [{"image": c["url"], "score": c["score"],
                      "w": c.get("w"), "h": c.get("h")} for c in measured[1:9]],
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
    hits.sort(key=lambda r: (r.get("name") or "").lower())

    cards = []
    for r in hits:
        alts = "".join(
            f'<a href="{escape(a["image"])}" target="_blank" title="score {a["score"]}">'
            f'<img loading="lazy" src="{escape(a["image"])}" alt=""></a>'
            for a in (r.get("alts") or [])
        )
        cards.append(
            '<figure class="c">'
            f'<img class="big" loading="lazy" src="{escape(r["image"])}" alt="">'
            f'<figcaption><b>{escape(r["name"] or "")}</b> '
            f'<span class="m">{escape(r.get("city") or "")}</span><br>'
            f'<span class="m">score {r.get("score")} · '
            f'{r.get("w") or "?"}×{r.get("h") or "?"}</span><br>'
            f'<a href="{escape(r["page"] or "")}" target="_blank">page</a> · '
            f'<a href="{escape(r["image"])}" target="_blank">image</a><br>'
            f'<code>{escape(r["venue_id"])}</code>'
            + (f'<div class="alts">{alts}</div>' if alts else "")
            + '</figcaption></figure>'
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
<title>OneTee course photos — review</title>
<style>
 body {{ font:15px/1.5 system-ui,sans-serif; margin:24px; background:#f6f5f2; color:#111; }}
 h1 {{ margin:0 0 4px; }} .sum {{ color:#555; margin-bottom:20px; max-width:60em; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:18px; }}
 .c {{ margin:0; background:#fff; border:1px solid #ddd; border-radius:10px; padding:10px; }}
 .c .big {{ width:100%; height:190px; object-fit:cover; background:#eee; border-radius:6px; }}
 figcaption {{ font-size:12px; margin-top:8px; }} .m {{ color:#666; }}
 code {{ font-size:11px; color:#888; }}
 .alts {{ display:flex; gap:6px; margin-top:8px; }}
 .alts img {{ width:64px; height:44px; object-fit:cover; border-radius:4px; border:1px solid #ddd; }}
 table {{ border-collapse:collapse; width:100%; margin-top:12px; background:#fff; }}
 td,th {{ border:1px solid #ddd; padding:6px 8px; font-size:13px; text-align:left; }}
</style>
<h1>Course photos — review</h1>
<p class="sum"><b>{len(hits)}</b> of <b>{len(rows)}</b> courses have a photo.
 The big picture is the pick; the small ones beside it are the runners-up, in
 score order. If the pick is wrong, click a runner-up to open it full size and
 note the venue id — swapping one is a single line in the override table.
 Anything with a person in it needs replacing: a filename cannot tell us that,
 only your eyes can.</p>
<div class="grid">{''.join(cards)}</div>
<h2>No photo found ({len(misses)})</h2>
<table><tr><th>Course</th><th>City</th><th>Website</th><th>Why</th></tr>{miss_rows}</table>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# --------------------------------------------------------------------------

def normalise(row: Dict) -> Dict:
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
    }


def fetch_directory() -> Dict:
    """Try each known directory host; report all of them if none answers."""
    problems = []
    for url in DIRECTORY_URLS:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            if r.status_code != 200:
                problems.append(f"{url} -> HTTP {r.status_code}")
                continue
            return r.json()
        except Exception as exc:
            problems.append(f"{url} -> {exc.__class__.__name__}")
    raise SystemExit("Could not reach the course directory.\n  " + "\n  ".join(problems))


def load_courses(state: str, only: Optional[str]) -> List[Dict]:
    payload = fetch_directory()

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
            raise SystemExit("Could not find the course list in /api/directory. "
                             "Top-level keys: " + ", ".join(sorted(payload.keys())))

    rows = [normalise(c) for c in raw if isinstance(c, dict)]
    if not any(r_["venue_id"] for r_ in rows):
        sample = sorted(raw[0].keys()) if raw and isinstance(raw[0], dict) else []
        raise SystemExit("No venue_id on any directory row — field names must have "
                         "changed. First row's keys: " + ", ".join(sample))

    if state:
        rows = [c for c in rows if c["state"] == state.upper()]
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        rows = [c for c in rows if c["venue_id"] in wanted]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Find a photo of each OneTee course.")
    ap.add_argument("--state", default="CO", help="two-letter state, blank for all")
    ap.add_argument("--only", default=None, help="comma-separated venue_ids")
    ap.add_argument("--only-file", default=None,
                    help="path to a JSON list of venue_ids — for re-running just "
                         "the courses whose photo was rejected by eye")
    ap.add_argument("--limit", type=int, default=0, help="stop after N courses (0 = all)")
    ap.add_argument("--workers", type=int, default=6, help="parallel courses")
    ap.add_argument("--out", default="data/course_photos.json")
    ap.add_argument("--review", default="data/course_photos_review.html")
    args = ap.parse_args()

    only = args.only
    if args.only_file:
        with open(args.only_file, encoding="utf-8") as fh:
            wanted = json.load(fh)
        only = ",".join(wanted if isinstance(wanted, list) else wanted.keys())
    courses = load_courses(args.state, only)
    if args.limit:
        courses = courses[:args.limit]
    if not courses:
        print("No courses matched.", file=sys.stderr)
        return 1

    print(f"Crawling {len(courses)} courses with {args.workers} workers…", flush=True)
    rows: List[Dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(find_photo_for, c) for c in courses]
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
        r["venue_id"]: {"image": r["image"], "page": r["page"],
                        "w": r["w"], "h": r["h"], "score": r["score"]}
        for r in rows if r.get("image")
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2, sort_keys=True)
    write_review(rows, args.review)

    print(f"\nDone. {len(table)}/{len(rows)} courses have a photo.")
    print(f"  table:  {args.out}")
    print(f"  review: {args.review}   <- open this before shipping anything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
