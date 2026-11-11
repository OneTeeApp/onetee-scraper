"""Browser fetcher for Chronogolf (Lightspeed Golf) public booking widgets.

WHY A BROWSER (2026-11-11). Since ~29 Sep the tee-times endpoint
(/marketplace/clubs/<id>/teetimes) answers Python `requests` with a Cloudflare
403 block page, while discovery (/private_api/clubs/...) still answers — which
is why every Chronogolf course failed with "teetimes endpoint failed" and the
platform went dark (148 courses). Probe run 42965653257 established it is the
client, not the runner's IP. See claude/chrono-teesnap-probe-2026-11-11.md.

WHAT THIS DOES. It drives a real, unmodified Chromium (stock Playwright, no
stealth patches, no fingerprint spoofing, headful under xvfb by default) to the
club's public booking widget — the same page a golfer opens, logged out — and
then asks the widget's own tee-times endpoint from inside that page, exactly the
request the widget makes when a golfer picks a date (captured live 2026-11-11 on
Collindale: GET /marketplace/clubs/3281/teetimes?date=…&course_id=3712
&affiliation_type_ids[]=13946&nb_holes=18). No login, no account, no CAPTCHA.
If Chronogolf's protection turns this browser away too, the run reports 403s and
lands nothing; it does not try to get around that.

One widget load per club serves every date, like browser_clubcaddie. Images,
fonts, media and third-party hosts (maps, consent, chat, analytics, payments)
are not fetched — the tee sheet needs none of them, and it keeps each club to a
few hundred KB.

PARTY SIZES. The API has no remaining-seats field; out_of_capacity is relative
to the requested party size. As in the plain adapter, ask at 4, 3, 2 and 1
players (the widget sends one affiliation_type_ids[] per player) and take the
largest party that still fits. The four requests for a course-date go out
together, as four tabs of the widget would.

This owns ALL chronogolf courses; scrape.yml, scrape-near.yml and scrape-far.yml
--exclude the platform, so the two never write the same course_slug in D1.

Usage:
    xvfb-run -a python -m scraper.browser_chronogolf --date 2026-11-12 --days 7 \
        --out-dir output [--shard 0/2]
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
from urllib.parse import urlparse

from .adapters.base import USER_AGENT
from .adapters.chronogolf import (BASE, ChronogolfAdapter, _disc_cache_get,
                                  _disc_cache_put)
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

PLATFORM = "chronogolf"
PARTY_SIZES = (4, 3, 2, 1)
DATE_GAP_MS = int(os.environ.get("CHRONO_DATE_GAP_MS", "150"))
CLUB_GAP_MS = int(os.environ.get("CHRONO_CLUB_GAP_MS", "800"))
SETTLE_MS = int(os.environ.get("CHRONO_SETTLE_MS", "1500"))

# Everything the tee sheet needs is served from www.chronogolf.com (measured on
# the live widget: 33 scripts, 11 XHRs, all first-party). Anything else is
# decoration or tracking.
_FIRST_PARTY = ("chronogolf.com",)
_SKIP_TYPES = {"image", "font", "media"}

DISCOVER_JS = r"""
async (key) => {
  const get = async (u) => {
    const r = await fetch(u, {credentials: 'include', headers: {'Accept': 'application/json'}});
    const t = await r.text();
    let j = null; try { j = JSON.parse(t); } catch (e) {}
    return {status: r.status, json: j};
  };
  const c = await get('/private_api/clubs/' + encodeURIComponent(key));
  if (c.status !== 200 || !c.json || c.json.id == null) return {ok: false, status: c.status};
  const club = c.json;
  const cs = await get('/private_api/clubs/' + club.id + '/courses');
  if (cs.status !== 200 || !cs.json) return {ok: false, status: cs.status, stage: 'courses'};
  const list = Array.isArray(cs.json) ? cs.json : (cs.json.courses || []);
  return {ok: true, club_id: club.id,
          aff: (club.settings || {}).default_affiliation_type_id || null,
          club_bookable: !!club.online_booking_enabled,
          courses: list.filter(x => x.online_booking_enabled)
                       .map(x => ({id: x.id, name: x.name || ''}))};
}
"""

TEETIMES_JS = r"""
async ({club_id, course_id, aff, date, sizes}) => {
  const one = async (n) => {
    const p = new URLSearchParams();
    p.append('date', date);
    p.append('course_id', String(course_id));
    for (let i = 0; i < n; i++) p.append('affiliation_type_ids[]', String(aff));
    p.append('nb_holes', '18');
    try {
      const r = await fetch('/marketplace/clubs/' + club_id + '/teetimes?' + p.toString(),
                            {credentials: 'include', headers: {'Accept': 'application/json'}});
      const t = await r.text();
      if (r.status !== 200) return {n, status: r.status, slots: null};
      try { const j = JSON.parse(t); return {n, status: 200, slots: Array.isArray(j) ? j : []}; }
      catch (e) { return {n, status: 200, slots: null, parse_failed: true}; }
    } catch (e) { return {n, status: -1, slots: null, error: String(e).slice(0, 80)}; }
  };
  return await Promise.all(sizes.map(one));
}
"""


def _route(route):
    req = route.request
    host = urlparse(req.url).hostname or ""
    if req.resource_type in _SKIP_TYPES or not host.endswith(_FIRST_PARTY):
        return route.abort()
    return route.continue_()


def _discover(page, key: str) -> dict | None:
    """club_id / affiliation / bookable course ids — cache-first, shared with the
    plain adapter's .cache/chronogolf_discovery.json (same shape)."""
    cached = _disc_cache_get(key)
    if cached and cached.get("club_id") is not None:
        names = {}
        for k, v in (cached.get("course_names") or {}).items():
            try:
                names[int(k)] = v
            except (TypeError, ValueError):
                names[k] = v
        return {**cached, "course_names": names}
    r = page.evaluate(DISCOVER_JS, key)
    if not r or not r.get("ok"):
        return None
    disc = {"club_id": r["club_id"], "affiliation_type_id": r.get("aff"),
            "club_bookable": bool(r.get("club_bookable")),
            "course_ids": [c["id"] for c in r.get("courses") or []],
            "course_names": {c["id"]: c.get("name", "") for c in r.get("courses") or []}}
    _disc_cache_put(key, disc)
    return disc


def _rows(course: dict, date: dt.date, cname: str, multi: bool,
          results: list) -> list:
    """Same reading as ChronogolfAdapter.fetch: a slot's open seats = the largest
    requested party it still fits."""
    by_slot: dict = {}
    for res in results:
        n = res.get("n")
        for slot in res.get("slots") or []:
            if slot.get("out_of_capacity"):
                continue
            sid = slot.get("id") or slot.get("uuid") or slot.get("start_time")
            e = by_slot.setdefault(sid, {"slot": slot, "spots": n})
            e["spots"] = max(e["spots"], n)
    out = []
    for e in by_slot.values():
        slot = e["slot"]
        start = slot.get("start_time", "")
        if not start:
            continue
        fees = [f.get("green_fee") for f in slot.get("green_fees", [])
                if isinstance(f.get("green_fee"), (int, float))]
        out.append(ChronogolfAdapter.base_tee_time(
            course,
            teetime=f"{slot.get('date', date.isoformat())}T{start}"
                    + ("" if len(start) > 5 else ":00"),
            course_label=cname if multi else "",
            holes=[18], open_spots=e["spots"],
            price_min=min(fees) if fees else None,
            price_max=max(fees) if fees else None,
            raw={"course_name": cname, **{k: slot.get(k) for k in
                 ("start_time", "hole", "out_of_capacity")}}))
    return out


def _fetch_club(ctx, course: dict, dates: list[dt.date]) -> dict:
    """-> {date_iso: {"tts": [...], "error": str|None, "failed_labels": [...]}}"""
    ids = course["ids"]
    key = str(ids.get("club_id") or ids.get("slug") or "")
    if not key:
        return {d.isoformat(): {"tts": [], "error": "no chronogolf slug/club_id",
                                "failed_labels": []} for d in dates}
    page = ctx.new_page()
    try:
        page.goto(f"{BASE}/club/{key}/widget?medium=widget&source=club",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(SETTLE_MS)       # let the widget's own scripts run
        disc = _discover(page, key)
        if disc is None:
            err = "discovery failed (club lookup did not answer)"
            return {d.isoformat(): {"tts": [], "error": err, "failed_labels": []}
                    for d in dates}
        problem = None
        if not disc["club_bookable"]:
            problem = ("unclaimed Chronogolf directory listing (club online_booking "
                       "disabled) — books through another engine")
        elif not disc["affiliation_type_id"]:
            problem = "no default_affiliation_type_id (contact-only / no online booking)"
        cids = ids.get("course_ids") or disc["course_ids"]
        if not problem and not cids:
            problem = "no online-bookable courses"
        if problem:
            return {d.isoformat(): {"tts": [], "error": problem, "failed_labels": []}
                    for d in dates}

        multi = len(cids) > 1
        out: dict = {}
        for d in dates:
            tts, failed, statuses = [], [], set()
            for cid in cids:
                res = page.evaluate(TEETIMES_JS, {
                    "club_id": disc["club_id"], "course_id": cid,
                    "aff": disc["affiliation_type_id"], "date": d.isoformat(),
                    "sizes": list(PARTY_SIZES)})
                statuses |= {r.get("status") for r in res}
                good = [r for r in res if r.get("slots") is not None]
                cname = disc["course_names"].get(cid, course["name"])
                if not good:            # all four sizes failed: unknown, not empty
                    failed.append(cname if multi else "")
                    continue
                tts.extend(_rows(course, d, cname, multi, good))
            err = None
            if failed and len(failed) == len(cids):
                err = "teetimes failed at every party size " + \
                      "(statuses " + ",".join(str(s) for s in sorted(statuses, key=str)) + ")"
                failed = []             # course-level error shields everything
            out[d.isoformat()] = {"tts": tts, "error": err,
                                  "failed_labels": failed if multi else []}
            page.wait_for_timeout(DATE_GAP_MS)
        return out
    except Exception as e:  # noqa: BLE001 — one club must not end the shard
        err = f"browser {type(e).__name__}"
        return {d.isoformat(): {"tts": [], "error": err, "failed_labels": []}
                for d in dates}
    finally:
        page.close()


def run(dates: list[dt.date], registry_path: str, out_dir: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry if c["platform"] == PLATFORM]
    courses = apply_shard(courses, shard)
    headless = os.environ.get("CHRONO_HEADLESS", "0") in ("1", "true", "True")
    log.info("browser-fetching %d chronogolf courses for %d dates (%s)",
             len(courses), len(dates), "headless" if headless else "headful")

    per: dict[str, dict] = {d.isoformat(): {} for d in dates}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        ctx.route("**/*", _route)
        for i, c in enumerate(courses):
            if i:
                time.sleep(CLUB_GAP_MS / 1000.0)
            t0 = time.monotonic()
            got = _fetch_club(ctx, c, dates)
            for diso, r in got.items():
                per[diso][c["slug"]] = r
            n = sum(len(r["tts"]) for r in got.values())
            errs = {r["error"] for r in got.values() if r["error"]}
            log.info("  %-40s %5d times  %4.1fs%s", c["slug"], n,
                     time.monotonic() - t0,
                     ("  ERROR " + "; ".join(sorted(errs))[:140]) if errs else "")
        browser.close()

    outp = pathlib.Path(out_dir)
    outp.mkdir(parents=True, exist_ok=True)
    paths = {}
    for d in dates:
        diso = d.isoformat()
        rs = per[diso]
        served = any(r["tts"] for r in rs.values())
        errors = [{"course": s, "platform": PLATFORM, "error": r["error"]}
                  for s, r in rs.items() if r["error"]]
        errors += [{"course": s, "platform": PLATFORM, "course_label": lbl,
                    "error": "sub-course teetimes failed"}
                   for s, r in rs.items() for lbl in r["failed_labels"]]
        doc = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": diso,
            "courses_queried": len(rs),
            "courses_ok": sum(1 for r in rs.values() if not r["error"]),
            "tee_times": [t.to_dict() for r in rs.values() for t in r["tts"]],
            # Same liveness guard as aggregate.py: a clean zero only counts as
            # "sold out / window closed" if some other Chronogolf course served
            # rows for this date in this run. If the whole platform came back
            # empty (a block that answers 200 with []), nothing is deactivated.
            "courses_empty": sorted(
                s for s, r in rs.items()
                if served and not r["error"] and not r["tts"]
                and not r["failed_labels"]),
            "errors": errors,
        }
        path = outp / f"chrono_{diso}.json"
        path.write_text(json.dumps(doc, indent=2))
        paths[diso] = str(path)
        log.info("wrote %s (%d tee times, %d errors, %d empty)", path,
                 len(doc["tee_times"]), len(errors), len(doc["courses_empty"]))
    return paths


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based Chronogolf fetcher")
    p.add_argument("--date", default=dt.date.today().isoformat(),
                   help="first date; --days controls how many")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out-dir", default="output")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    start = dt.date.fromisoformat(a.date)
    dates = [start + dt.timedelta(days=n) for n in range(a.days)]
    run(dates, a.registry, a.out_dir, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
