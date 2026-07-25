"""Second pass at the sites that refused the plain HTTP probe.

native_probe.py got HTTP 403 from a WAF on 17 course websites (14 Arizona,
3 Colorado) and a 1.5KB JS-only shell from suncityaz.org, so for those the
report says nothing about whether a native booking engine exists. This loads
the same pages in a real headless Chromium — the permitted move: it is an
ordinary browser fetching a public page, and a managed challenge that clears
on its own is fine.

Explicitly NOT done here: no CAPTCHA or interactive "verify you are human"
challenge is solved, no TLS fingerprint is forged, no credentials are entered,
and any page that turns out to need a login is reported and left alone. A page
still showing a challenge after load is reported as CHALLENGE and skipped.

Report only. Nothing here edits the CSV or the registry.

Usage: python scripts/browser_native_probe.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from playwright.sync_api import sync_playwright  # noqa: E402

from native_probe import ENGINES, NATIVE, BOOK_HINT, TAG_RE, scan  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CHALLENGE = ("just a moment", "verify you are human", "checking your browser",
             "enable javascript and cookies", "attention required",
             "cf-challenge", "px-captcha", "are you a robot")
LOGIN = ("sign in", "log on", "member login", "password")

# (state, course name, url). Everything native_az.txt / native_co.txt could
# not answer, minus three that need no browser:
#   Quail Canyon      — domain expired, now redirects to an unrelated site
#   Palo Duro Creek   — only a Facebook page, and FB wants a login
#   Cocopah Rio Colorado — no website in the CSV at all
TARGETS = [
    # --- Colorado (#67 stragglers)
    ("CO", "Homestead Golf Course", "https://www.golflakewood.com"),
    ("CO", "Rollingstone Ranch Golf Club",
     "https://www.rollingstoneranchgolf.com"),
    ("CO", "University of Denver Golf Club at Highlands Ranch",
     "https://highlandsranchgolf.du.edu"),
    ("CO", "Emerald Greens Golf Club", "https://www.windsorgardensdenver.org"),

    # --- Arizona: WAF-blocked (#68)
    ("AZ", "SunBird Golf Club", "https://www.sunbirdgolf.com/"),
    ("AZ", "Desert Mirage Golf Course", "https://www.golfdm.com"),
    ("AZ", "Viewpoint Golf Resort", "https://www.viewpointgolfresort.com/"),
    ("AZ", "Sun City Country Club", "https://www.suncitycountryclub.org"),
    ("AZ", "Santa Rita Golf Club", "https://santaritagolf.com"),
    ("AZ", "The Preserve Golf Club at SaddleBrooke",
     "https://www.golfthepreserve.com"),
    ("AZ", "Twin Lakes Golf Course", "https://cityofwillcox.org/golf/"),
    ("AZ", "Los Lagos Golf Club", "https://www.loslagoslinks.com/"),
    ("AZ", "Valle Vista Golf Club", "https://www.vallevistart66az.com/"),
    ("AZ", "Desert Hills Golf Course", "https://www.yumaaz.gov/"),
    ("AZ", "Mission Royale Golf Club", "https://www.missionroyalegolfclub.com/"),
    ("AZ", "Palm Creek Golf & RV Resort",
     "https://www.sunoutdoors.com/arizona/palm-creek-resort-residences/golf"),
    ("AZ", "Robson Ranch Golf Club", "https://www.robsonranchgolf.com/"),

    # --- Arizona: answered 200 but rendered nothing useful to plain HTTP
    ("AZ", "RCSC (8 Sun City courses)", "https://suncityaz.org"),
    ("AZ", "Westbrook Village Golf Club", "https://www.westbrookvillagegolf.com"),
    ("AZ", "Wickenburg Ranch Golf & Social Club",
     "https://www.wickenburgranch.com/golf"),
    ("AZ", "Pinewood Country Club", "https://pinewoodcountryclubaz.com/"),
    ("AZ", "Mountain View Golf Course (Fort Huachuca)",
     "https://huachuca.armymwr.com/programs/mountain-view-golf-course"),
    ("AZ", "Douglas Municipal Golf Course", "https://douglasaz.gov/"),
]

MAX_LINKS = 3


def in_page_links(page, base_host: str) -> list[tuple[str, str]]:
    """(href, text) for same-host links whose text or href looks bookable."""
    try:
        raw = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [e.href, (e.textContent||'').trim().slice(0,80)])")
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in raw:
        if not href.startswith("http") or href in seen:
            continue
        if base_host not in href:
            continue
        if not BOOK_HINT.search(text or "") and not BOOK_HINT.search(href):
            continue
        seen.add(href)
        out.append((href, TAG_RE.sub(" ", text or "").strip()))
        if len(out) >= MAX_LINKS:
            break
    return out


def load(page, url: str) -> tuple[int | None, str, str]:
    """-> (status, final_url, html). status None means navigation failed."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", ""
    try:
        page.wait_for_timeout(3500)          # let a managed challenge clear
    except Exception:  # noqa: BLE001
        pass
    try:
        html = page.content()
    except Exception:  # noqa: BLE001
        html = ""
    return (resp.status if resp else None), page.url, html


def probe(page, state: str, name: str, url: str) -> None:
    print(f"\n--- [{state}] {name}")
    print(f"    url: {url}")
    status, final, html = load(page, url)
    if not html:
        print(f"    RESULT: UNREACHABLE ({final})")
        return
    low = html.lower()
    print(f"    HTTP {status} {len(html)}B  final={final}")
    hit_challenge = [c for c in CHALLENGE if c in low]
    if hit_challenge and len(html) < 20000:
        print(f"    RESULT: CHALLENGE (page still showing {hit_challenge[0]!r}) "
              "— left alone, not solved")
        return

    where = {k: ("homepage", v) for k, v in scan(html).items()}
    if not any(k in where for k in NATIVE):
        host = final.split("/")[2] if "//" in final else ""
        for href, text in in_page_links(page, host):
            s2, f2, h2 = load(page, href)
            print(f"    -> {href} ({text!r}): HTTP {s2} {len(h2)}B")
            if not h2:
                continue
            if any(m in h2.lower()[:8000] for m in LOGIN):
                print("       (page mentions sign-in)")
            for k, v in scan(h2).items():
                where.setdefault(k, (href, v))
            if any(k in where for k in NATIVE):
                break

    for k, (src, urls) in sorted(where.items()):
        for u in sorted(urls):
            print(f"    hit {k:12s} {u}   [{'homepage' if src == 'homepage' else 'linked'}]")

    native = sorted(k for k in where if k in NATIVE)
    if native:
        print("    RESULT: NATIVE -> " + ", ".join(native))
    elif "golfnow" in where:
        print("    RESULT: GOLFNOW-ONLY (site itself links to GolfNow)")
    else:
        print("    RESULT: NONE-FOUND (phone/walk-in, or an engine we do not "
              "recognise)")


def main() -> None:
    print(f"browser_native_probe: {len(TARGETS)} sites that refused plain HTTP")
    print("Real headless Chromium on public pages. No challenge is solved, no "
          "credentials are entered, no TLS fingerprint is forged.")
    print("Report only. Nothing here edits the CSV or the registry.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(30000)
        for state, name, url in TARGETS:
            try:
                probe(page, state, name, url)
            except Exception as exc:  # noqa: BLE001
                print(f"    HARNESS ERROR: {type(exc).__name__}: {str(exc)[:160]}")
            sys.stdout.flush()
        ctx.close()
        browser.close()
    print("\ndone")


if __name__ == "__main__":
    main()
