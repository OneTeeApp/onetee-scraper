"""Find the NATIVE booking engine behind a course's own website.

Two jobs, one scanner:

  MODE=az  (default) — the 41 Arizona courses currently tagged `golfnow`.
    GolfNow is a reseller: it lists a subset of a course's inventory at
    marked-up or restricted times. Where the course also runs its own engine
    (ForeUp, TeeItUp, Chronogolf, Club Prophet, Teesnap, ...) we want the
    native engine as `primary` and GolfNow kept only as a deduped `supplement`
    sharing the venue_id.

  MODE=co  — the Colorado stragglers whose registered booking host answers but
    is not a real tenant. universityofdenver / emeraldgreens / eagletrace
    .cps.golf all 404 on /identityapi/myconnect/token/short where a working
    tenant (indianpeaks) 400s, i.e. the identity app is not deployed there at
    all. Their real booking link should be on their own website.

Method: fetch the course's Website, regex the HTML for known booking-engine
URLs, and if none appear follow up to MAX_LINKS same-page links whose text or
href looks like a booking entry point. Report only — this NEVER edits the CSV
or the registry, because a wrong auto-registration publishes another course's
tee sheet under the wrong name (which is exactly how Meadows/Foothills and the
three shared-golfClubId courses went wrong).

Chronogolf needs one extra check: chronogolf.com publishes DIRECTORY pages for
courses that are not its customers. Those pages carry `no-online-booking-pin`
and must not be registered — douglas-golf-course-arizona and
palo-duro-creek-golf-course are both this trap.

Public pages only: no credentials, no CAPTCHA solving, no TLS fingerprinting.
A page that needs a login is reported as LOGIN and left alone.
"""
from __future__ import annotations

import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MODE = os.environ.get("MODE", "az").lower()
MAX_LINKS = int(os.environ.get("MAX_LINKS", "4"))
WORKERS = int(os.environ.get("WORKERS", "4"))

# Booking engines we already have (or could write) an adapter for. Order
# matters only for reporting; every hit is reported.
ENGINES: dict[str, re.Pattern] = {
    "foreup":      re.compile(r"foreupsoftware\.com/[^\s\"'<>]*", re.I),
    "teeitup":     re.compile(r"[a-z0-9-]+\.(?:book|play)\.teeitup\.(?:com|golf)[^\s\"'<>]*", re.I),
    "chronogolf":  re.compile(r"(?:www\.)?chronogolf\.(?:com|ca)/[^\s\"'<>]*", re.I),
    "clubprophet": re.compile(r"[a-z0-9-]+\.cps\.golf[^\s\"'<>]*", re.I),
    "prophetsvc":  re.compile(r"secure\.[a-z]+\.prophetservices\.com/[^\s\"'<>]*", re.I),
    "teesnap":     re.compile(r"[a-z0-9-]+\.teesnap\.net[^\s\"'<>]*", re.I),
    "quick18":     re.compile(r"[a-z0-9-]+\.(?:quick18|play18)\.com[^\s\"'<>]*", re.I),
    "membersports": re.compile(r"(?:app\.)?membersports\.com/[^\s\"'<>]*", re.I),
    "clubcaddie":  re.compile(r"(?:[a-z0-9-]+\.)?clubcaddie\.com[^\s\"'<>]*", re.I),
    "ezlinks":     re.compile(r"(?:[a-z0-9-]+\.)?ezlinks(?:golf)?\.com[^\s\"'<>]*", re.I),
    "foretees":    re.compile(r"[a-z0-9-]+\.foretees\.com[^\s\"'<>]*", re.I),
    "noteefy":     re.compile(r"(?:app\.)?noteefy\.app[^\s\"'<>]*", re.I),
    "supersaas":   re.compile(r"(?:www\.)?supersaas\.com/schedule[^\s\"'<>]*", re.I),
    "totale":      re.compile(r"[a-z0-9-]+\.totaleintegrated\.net[^\s\"'<>]*", re.I),
    "golfback":    re.compile(r"[a-z0-9-]+\.golfback\.com[^\s\"'<>]*", re.I),
    "golfrev":     re.compile(r"(?:[a-z0-9-]+\.)?golfrev\.com[^\s\"'<>]*", re.I),
    "lightspeed":  re.compile(r"(?:www\.)?lightspeedhq\.com/golf[^\s\"'<>]*", re.I),
    "golfnow":     re.compile(r"(?:www\.)?golfnow\.com/[^\s\"'<>]*", re.I),
}
# golfnow is where these rows already are, so a golfnow-only hit is "no change".
NATIVE = [k for k in ENGINES if k != "golfnow"]

BOOK_HINT = re.compile(r"book|tee[-\s]?time|reserve|golf|rates", re.I)
LOGIN_HINT = re.compile(r"\b(sign in|log ?on|log ?in|password|member login)\b", re.I)
LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.{0,120}?)</a>',
                     re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def fetch(s: requests.Session, url: str) -> tuple[int, str, str]:
    """-> (status, final_url, text). status 0 means the request blew up."""
    try:
        r = s.get(url, timeout=25, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        text = r.text if "html" in ct or "text" in ct else ""
        return r.status_code, r.url, text
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", ""


def scan(html: str) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for name, pat in ENGINES.items():
        found = {m.rstrip(".,);'\"") for m in pat.findall(html)}
        # chronogolf.com/club/... directory pages and generic marketing links
        # to an engine's homepage are not booking links; require some path.
        found = {f for f in found if len(f) > 12}
        if found:
            hits[name] = set(sorted(found)[:3])
    return hits


def chronogolf_is_directory(s: requests.Session, url: str) -> bool | None:
    """True = directory-only listing (NOT a customer), None = could not tell."""
    if not url.startswith("http"):
        url = "https://" + url
    code, _, html = fetch(s, url)
    if code != 200 or not html:
        return None
    return "no-online-booking-pin" in html


def booking_links(base_url: str, html: str) -> list[str]:
    out: list[str] = []
    host = urlparse(base_url).netloc
    for href, label in LINK_RE.findall(html):
        text = TAG_RE.sub(" ", label).strip()
        if not BOOK_HINT.search(text) and not BOOK_HINT.search(href):
            continue
        full = urljoin(base_url, href)
        if not full.startswith("http"):
            continue
        # stay on the course's own site; off-site engine links were already
        # caught by scan() on the homepage HTML
        if urlparse(full).netloc != host:
            continue
        if full.rstrip("/") == base_url.rstrip("/"):
            continue
        if full not in out:
            out.append(full)
        if len(out) >= MAX_LINKS:
            break
    return out


def probe(row: dict) -> str:
    name = row["Course Name"].strip()
    site = (row.get("Website") or "").strip()
    lines = [f"\n--- {name}"]
    lines.append(f"    website: {site or '(none in CSV)'}")
    if not site:
        lines.append("    RESULT: NO-WEBSITE — needs a manual look")
        return "\n".join(lines)
    if not site.startswith("http"):
        site = "https://" + site

    s = session()
    code, final, html = fetch(s, site)
    lines.append(f"    homepage: HTTP {code} {len(html)}B  final={final}")
    if code == 0:
        lines.append("    RESULT: UNREACHABLE")
        return "\n".join(lines)
    if code >= 400 or not html:
        lines.append(f"    RESULT: SITE-{code}")
        return "\n".join(lines)

    hits = scan(html)
    where = {k: ("homepage", v) for k, v in hits.items()}

    if not any(k in hits for k in NATIVE):
        for link in booking_links(final, html):
            c2, f2, h2 = fetch(s, link)
            lines.append(f"    -> {link}: HTTP {c2} {len(h2)}B")
            if c2 != 200 or not h2:
                continue
            if LOGIN_HINT.search(h2[:6000]):
                lines.append("       (page mentions sign-in)")
            for k, v in scan(h2).items():
                if k not in where:
                    where[k] = (link, v)
            if any(k in where for k in NATIVE):
                break

    native = {k: v for k, v in where.items() if k in NATIVE}
    for k, (src, urls) in sorted(where.items()):
        for u in sorted(urls):
            lines.append(f"    hit {k:12s} {u}   [{'homepage' if src == 'homepage' else 'linked'}]")

    if "chronogolf" in native:
        u = sorted(native["chronogolf"][1])[0]
        d = chronogolf_is_directory(s, u)
        lines.append(f"    chronogolf directory-only? {d}")
        if d:
            native.pop("chronogolf")
            lines.append("    (chronogolf hit dropped: directory listing, "
                         "not a customer)")

    if native:
        lines.append("    RESULT: NATIVE -> " + ", ".join(sorted(native)))
    elif "golfnow" in where:
        lines.append("    RESULT: GOLFNOW-ONLY (site itself links to GolfNow)")
    else:
        lines.append("    RESULT: NONE-FOUND (phone/walk-in, or an engine we "
                     "do not recognise)")
    return "\n".join(lines)


def rows_for_mode() -> list[dict]:
    if MODE == "az":
        rows = list(csv.DictReader(open("arizona_golf_courses_booking.csv")))
        return [r for r in rows
                if (r.get("Booking Platform") or "").strip().lower() == "golfnow"]
    rows = list(csv.DictReader(open("colorado_golf_courses_booking.csv")))
    want = {"university of denver golf club at highlands ranch",
            "emerald greens golf club", "eagle trace golf club",
            "lake arbor golf club", "homestead golf course",
            "rollingstone ranch golf club", "the meadows golf club",
            "meadows golf club"}
    return [r for r in rows if r["Course Name"].strip().lower() in want]


def main() -> None:
    rows = rows_for_mode()
    print(f"native_probe MODE={MODE}: {len(rows)} courses, "
          f"{WORKERS} workers, up to {MAX_LINKS} followed links each")
    print("Reports only. Nothing here edits the CSV or the registry.")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for out in ex.map(probe, rows):
            print(out)
            sys.stdout.flush()
    print("\ndone")


if __name__ == "__main__":
    main()
