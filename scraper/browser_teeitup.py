"""Headless-browser far-horizon fetcher for TeeItUp (kenna.io backend).

WHY THIS EXISTS. The plain TeeItUp adapter (adapters/teeitup.py) talks to
`phx-api-be-east-1b.kenna.io` with a python-requests client. kenna soft-throttles
that traffic: partway through a far sweep it starts answering the data-center
runner with empty-200s, so the mid/far band (day ~12-22) collapses to zero for
most courses even though the inventory is really there. Measured decisively on
2026-07-27: `coldwater-golf-club` had 0 rows in D1 at day +14 while its own
booking site (kenna, from a residential browser) served 90 tee times for the
same date.

WHAT'S DIFFERENT HERE. This runs a REAL headless Chromium and fires kenna from
inside a live `*.book.teeitup.com` page — the exact context the booking SPA uses
— so kenna sees a browser TLS fingerprint and a legitimate booking Origin/Referer
rather than a bare python client. kenna's CORS is permissive across teeitup
booking origins (verified: from the coldwater origin, `x-be-alias: city-of-
phoenix-golf-courses` returns that alias's 7 facilities), so ONE page load serves
the whole fleet cross-alias. Requests are paced to stay under kenna's burst
limit.

ANSWERED 2026-08-04: a data-center Actions runner is NOT enough. kenna now
blocks the runner's Chromium at the network/CORS layer ("Failed to fetch" on
~87 courses a pass) and 429s the plain client, while the identical calls succeed
from a residential browser. The remaining lever is a residential proxy: set the
`TEEITUP_PROXY` env var (a full URL, optionally with credentials) and run()
passes it to chromium.launch(proxy=...) below. Chromium ignores HTTPS_PROXY on
its own, so it MUST go through launch(proxy=), which is what the code now does.

OWNERSHIP. This owns the FAR window for teeitup. The plain far tier
(scrape-far.yml) excludes `teeitup`, so the two never write the same
course_slug+date and clobber each other in D1. The near/mid tiers keep scraping
teeitup with the plain adapter (kenna serves near dates fine).

Parsing is reused verbatim from TeeItUpAdapter._parse — this file only replaces
the transport, never the ownership/timezone/sub-course-label logic.

Usage:
    python -m scraper.browser_teeitup --date 2026-08-10 --out output/tu.json
    python -m scraper.browser_teeitup --date 2026-08-10 --shard 0/4 --limit 5
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import urllib.parse

from .adapters.base import USER_AGENT
from .adapters.teeitup import TeeItUpAdapter, API_BASE
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# Fetch kenna facilities for an alias, from inside the booking page (real browser
# TLS + a legitimate teeitup Origin). Returns the raw list or an error marker.
FAC_JS = r"""
async ([apiBase, alias]) => {
  try {
    const r = await fetch(apiBase + "/alias/" + alias + "/facilities",
      {headers: {"x-be-alias": alias}});
    if (r.status !== 200) return {status: r.status, facilities: []};
    const j = await r.json();
    const list = Array.isArray(j) ? j : (j.courses || []);
    return {status: 200, facilities: list};
  } catch (e) { return {status: -1, error: String(e).slice(0,120), facilities: []}; }
}
"""

# Fetch one alias's tee-times for one date. facilityIds is sent when the registry
# pins it (a course sharing a multi-facility alias); kenna honours it server-side.
TT_JS = r"""
async ([apiBase, alias, facilityIds, date]) => {
  try {
    let url = apiBase + "/v2/tee-times?date=" + date;
    if (facilityIds) url += "&facilityIds=" + encodeURIComponent(facilityIds);
    const r = await fetch(url, {headers: {"x-be-alias": alias}});
    // Read text first so a non-JSON throttle page is reported as a failure,
    // never silently swallowed into an empty (which would look like "0 slots").
    const text = await r.text();
    let j;
    try { j = JSON.parse(text); }
    catch (e) { return {status: r.status, parse_failed: true, bytes: text.length}; }
    const blocks = Array.isArray(j) ? j : [j];
    let n = 0; for (const b of blocks) n += ((b && b.teetimes) || []).length;
    return {status: r.status, blocks, slots: n};
  } catch (e) { return {status: -1, error: String(e).slice(0,120)}; }
}
"""

# kenna pacing from inside the browser. Lighter than the plain adapter's 0.7s
# (a browser context is treated more gently), but non-zero so a whole-fleet
# sweep from one page does not burst-429 itself.
_GAP_MS = 350


def _load_origin(page, courses) -> str | None:
    """Open a live teeitup booking page to get a CORS-legitimate origin.

    Cross-alias works, so any one booking origin can query the whole fleet; we
    just need one that actually loads. Try each course's booking URL in turn.
    """
    for c in courses[:8]:
        alias = c["ids"].get("alias")
        if not alias:
            continue
        url = c.get("booking_url") or f"https://{alias}.book.teeitup.com/"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(1500)   # let any managed challenge settle
            return url
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
    return None


def run(date: dt.date, registry_path: str, out_path: str,
        shard: str | None = None, limit: int | None = None,
        only: set[str] | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry
               if c["platform"] == "teeitup" and c["ids"].get("alias")]
    if only:
        courses = [c for c in courses if c["slug"] in only]
    courses = apply_shard(courses, shard)
    if limit:
        courses = courses[:limit]
    log.info("browser-fetching %d teeitup courses for %s", len(courses), date)

    adapter = TeeItUpAdapter()          # class caches are shared across courses
    fac_cache: dict[str, list] = {}     # alias -> facilities (per-run)
    tee_times, errors = [], []
    served = throttled = 0

    with sync_playwright() as pw:
        # Residential proxy (the only lever left once kenna blocks the
        # data-center IP — measured 2026-08-04: kenna refuses the runner's
        # Chromium fetches with a network/CORS "Failed to fetch" on ~87 courses
        # a pass, and 429s the plain client, while the same calls succeed from a
        # residential browser). Playwright's Chromium does NOT honour the
        # HTTPS_PROXY env var on its own — it must be passed to launch(proxy=).
        # TEEITUP_PROXY is a full URL, optionally with credentials, e.g.
        # http://user:pass@host:port ; unset = direct connection (unchanged).
        launch_kwargs = {"args": ["--no-sandbox"]}
        _proxy_url = os.environ.get("TEEITUP_PROXY", "").strip()
        if _proxy_url:
            _pu = urllib.parse.urlparse(_proxy_url)
            _server = f"{_pu.scheme}://{_pu.hostname}"
            if _pu.port:
                _server += f":{_pu.port}"
            _proxy = {"server": _server}
            if _pu.username:
                _proxy["username"] = urllib.parse.unquote(_pu.username)
            if _pu.password:
                _proxy["password"] = urllib.parse.unquote(_pu.password)
            launch_kwargs["proxy"] = _proxy
            log.info("teeitup: routing Chromium through proxy %s", _server)
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page(user_agent=USER_AGENT)
        origin = _load_origin(page, courses)
        if not origin:
            browser.close()
            raise RuntimeError("could not load any teeitup booking origin")

        for c in courses:
            alias = c["ids"]["alias"]
            fid = c["ids"].get("facility_id")

            # facilities: once per alias per run (shared aliases reuse it).
            # Cache SUCCESSES only — remembering a failed call as [] pinned
            # "no facilities" on the alias for the whole run (no labels, state
            # tz fallback, and a pinned course could resolve to zero slots).
            if alias not in fac_cache:
                fr = page.evaluate(FAC_JS, [API_BASE, alias])
                got = fr.get("facilities") or []
                if got or fr.get("status") == 200:
                    fac_cache[alias] = got
                else:
                    log.info("  %-40s facilities fetch failed (%s), not cached",
                             c["slug"], fr.get("status"))
                page.wait_for_timeout(_GAP_MS)
            facilities = fac_cache.get(alias, [])

            tr = page.evaluate(TT_JS, [API_BASE, alias,
                                       str(fid) if fid else "", date.isoformat()])
            page.wait_for_timeout(_GAP_MS)

            status = tr.get("status")
            # parse_failed can arrive WITH status 200 — kenna's soft throttle
            # serves an HTML interstitial on a 200. Gating only on the status
            # counted those as "served, 0 slots" and deactivated real rows.
            if status != 200 or tr.get("parse_failed"):
                note = ("throttle/parse" if tr.get("parse_failed")
                        else f"status {status}" + (
                            f" {tr.get('error')}" if tr.get("error") else ""))
                errors.append({"course": c["slug"], "platform": "teeitup",
                               "error": f"browser {note}"})
                throttled += 1
                log.info("  %-40s ERROR %s", c["slug"], note)
                continue

            # Prime the adapter's facilities cache so _parse resolves offline,
            # then reuse the exact ownership + tz + label logic.
            adapter._FACILITIES[alias] = facilities
            try:
                tts = adapter._parse(c, tr.get("blocks") or [])
            except Exception as e:  # noqa: BLE001 — parse fault on one course
                errors.append({"course": c["slug"], "platform": "teeitup",
                               "error": f"parse {type(e).__name__}: {e}"})
                log.info("  %-40s PARSE-ERR %s", c["slug"], type(e).__name__)
                continue
            tee_times.extend(tts)
            served += 1
            log.info("  %-40s %d times (kenna %d slots)",
                     c["slug"], len(tts), tr.get("slots", 0))

        browser.close()

    log.info("teeitup browser %s: served=%d throttled=%d of %d",
             date, served, throttled, len(courses))
    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "courses_queried": len(courses),
        "courses_ok": served,
        "tee_times": [t.to_dict() for t in tee_times],
        "errors": errors,
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based TeeItUp far fetcher")
    p.add_argument("--date",
                   default=(dt.date.today() + dt.timedelta(days=14)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--limit", type=int, help="only the first N courses (probe)")
    p.add_argument("--courses", help="comma-separated slugs to restrict to (probe)")
    p.add_argument("--out", default="output/tu.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    only = {s.strip() for s in a.courses.split(",") if s.strip()} if a.courses else None
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard, a.limit, only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
