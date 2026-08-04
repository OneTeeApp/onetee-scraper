"""Headless-browser fetcher for Golf With Access (Troon's golfwithaccess.com).

WHY. As of ~2026-08 the public JSON endpoint
`/api/v1/tee-times?courseIds=…` returns a clean HTTP 200 with an EMPTY
`teeTimes:[]` to our plain client — even from a residential IP and even when the
request is byte-identical to the one the site's own SPA makes. The live SPA gets
full data (Tucson City Golf renders ~270 slots/day) because its bundled HTTP
client attaches a signed/session value we cannot see or replay from outside the
page. So all 22 Golf With Access courses silently went dark on the OneTee feed
(the empty-200 reads as "sold out", not an error). Probed and confirmed live
2026-08-04.

THE FIX (this file). Load the real reserve page in a headless Chromium and read
the response to the SPA'S OWN tee-times request — the app makes it WITH its
header, and Playwright reads the body. No header reverse-engineering, no DOM
scraping fragility. The parsed slots are handed to the existing
GolfWithAccessAdapter parsing helpers. This owns ALL golfwithaccess courses (the
plain near/mid/far tiers now EXCLUDE the platform) so the two never write the
same course_slug+date and clobber each other in D1.

OWNERSHIP RESOLUTION. One reserve page ("/course/<tenant>/reserve-tee-time")
serves every course under a tenant (Tucson City Golf = 5 munis in one load), so
the response mixes courses. Each slot carries course.{id,slug,name}; we assign a
slot to a registry course by (1) pinned course_id, then (2) course.slug ==
registry slug/venue_id, then (3) if the tenant has exactly one registry course,
that course. Slots matching none are dropped (logged). This mirrors the plain
adapter's wrong-id discipline: never publish a slot we cannot attribute.

Usage:
    python -m scraper.browser_golfwithaccess --date 2026-08-09 --out output/gwa.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

from .adapters.base import USER_AGENT
from .adapters.golfwithaccess import GolfWithAccessAdapter, BOOKING_PAGE
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

TEE_TIMES_MARKER = "/api/v1/tee-times"
RESERVE_URL = "https://golfwithaccess.com/course/{tenant}/reserve-tee-time?date={date}&players=2"


def _norm_slug(s: str | None) -> str:
    """Normalize a course slug for fuzzy matching: lowercase and collapse the
    "-and-"/"&" that Golf With Access inserts in combined-course slugs but our
    registry omits (king-and-bear vs king-bear)."""
    return (s or "").lower().replace("-and-", "-").replace("-&-", "-").replace("&", "")


def _slot_key(s: dict) -> tuple:
    """Dedupe key for a raw slot across repeated responses."""
    c = (s.get("course") or {}).get("id")
    d = s.get("dayTime") or {}
    return (c, d.get("year"), d.get("month"), d.get("day"),
            d.get("hour"), d.get("minute"), s.get("holesOption"))


def _capture_tenant(page, tenant: str, date: dt.date) -> tuple[list[dict], str | None]:
    """Navigate a tenant's reserve page and return the merged, de-duped raw
    teeTimes captured from the SPA's own request(s). Second element is an error
    string on failure, else None."""
    url = RESERVE_URL.format(tenant=tenant, date=date.isoformat())
    for attempt in range(3):
        resps: list = []
        handler = lambda r: resps.append(r) if TEE_TIMES_MARKER in r.url else None
        page.on("response", handler)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # Let the SPA boot, clear any managed Cloudflare JS check, and fire
            # its tee-times request(s). networkidle would be ideal but the page
            # keeps long-poll/analytics sockets open, so use a fixed settle.
            page.wait_for_timeout(8000)
        except Exception as e:  # noqa: BLE001
            page.remove_listener("response", handler)
            if attempt == 2:
                return [], type(e).__name__
            page.wait_for_timeout(3000 * (attempt + 1))
            continue
        page.remove_listener("response", handler)

        merged: dict[tuple, dict] = {}
        seen_200 = False
        for r in resps:
            try:
                if r.status != 200:
                    continue
                seen_200 = True
                body = r.json()
            except Exception:  # noqa: BLE001
                continue
            for s in (body or {}).get("teeTimes") or []:
                merged[_slot_key(s)] = s
        if merged:
            return list(merged.values()), None
        # No slots. A 200-with-empty is a legitimate "no inventory" only if we
        # actually saw the request succeed; if we saw NO tee-times response at
        # all, the page never fired it (challenge / route change) — retry.
        if seen_200:
            return [], None
        page.wait_for_timeout(3000 * (attempt + 1))
    return [], "no tee-times request observed"


def _resolve(courses: list[dict], slots: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    """Assign each slot to at most one registry course. Returns (slug -> slots,
    diag)."""
    by_id: dict[str, dict] = {}
    slug_index: dict[str, dict] = {}
    norm_index: dict[str, dict] = {}
    for c in courses:
        cid = (c.get("ids") or {}).get("course_id")
        if cid:
            by_id[cid] = c
        for k in {c.get("slug"), c.get("venue_id")}:
            if k:
                slug_index.setdefault(k, c)
                norm_index.setdefault(_norm_slug(k), c)
    sole = courses[0] if len(courses) == 1 else None

    owned: dict[str, list[dict]] = {c["slug"]: [] for c in courses}
    unresolved = 0
    for s in slots:
        sc = s.get("course") or {}
        c = by_id.get(sc.get("id"))
        if c is None:
            c = slug_index.get(sc.get("slug"))
        # Normalized slug fallback: Golf With Access spells combined-course
        # slugs with "-and-" ("world-golf-village-king-and-bear") where our
        # registry uses "-" ("world-golf-village-king-bear"). Only fires after
        # exact id/slug fail, and only matches within this tenant's courses, so
        # it cannot cross-attribute to another venue.
        if c is None:
            c = norm_index.get(_norm_slug(sc.get("slug")))
        if c is None and sole is not None:
            c = sole
        if c is None:
            unresolved += 1
            continue
        owned[c["slug"]].append(s)
    return owned, {"unresolved": unresolved,
                   "distinct_ids": len({(s.get("course") or {}).get("id") for s in slots})}


def run(date: dt.date, registry_path: str, out_path: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry if c.get("platform") == "golfwithaccess"]
    courses = apply_shard(courses, shard)
    tenants: dict[str, list[dict]] = {}
    for c in courses:
        tenant = (c.get("ids") or {}).get("tenant")
        if not tenant:
            log.info("  %-34s SKIP (no tenant pinned)", c["slug"])
            continue
        tenants.setdefault(tenant, []).append(c)
    log.info("browser-fetching %d golfwithaccess tenants (%d courses) for %s",
             len(tenants), len(courses), date)

    tee_times, errors = [], []
    ok_slugs: set[str] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        for i, (tenant, tcourses) in enumerate(tenants.items()):
            if i:
                page.wait_for_timeout(1500)     # pace between tenants
            slots, err = _capture_tenant(page, tenant, date)
            if err:
                for c in tcourses:
                    errors.append({"course": c["slug"], "platform": "golfwithaccess",
                                   "error": f"browser {err}"})
                log.info("  tenant %-30s ERROR %s", tenant, err)
                continue
            owned, diag = _resolve(tcourses, slots)
            if diag["unresolved"]:
                log.info("  tenant %-30s %d slots, %d distinct ids, %d UNRESOLVED",
                         tenant, len(slots), diag["distinct_ids"], diag["unresolved"])
            for c in tcourses:
                cslots = owned.get(c["slug"], [])
                booking_url = (c.get("booking_url")
                               or BOOKING_PAGE.format(tenant=tenant))
                tts = [GolfWithAccessAdapter._slot_to_teetime(c, s, booking_url, date)
                       for s in cslots]
                tts = [t for t in tts if t is not None]
                tee_times.extend(tts)
                ok_slugs.add(c["slug"])
                log.info("  %-34s %d times", c["slug"], len(tts))
        browser.close()

    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "courses_queried": len(courses),
        "courses_ok": len(ok_slugs),
        "tee_times": [t.to_dict() for t in tee_times],
        "errors": errors,
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based Golf With Access fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/gwa.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
