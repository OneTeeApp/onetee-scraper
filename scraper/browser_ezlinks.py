"""Headless-browser fetcher for EZLinks portals (*.ezlinksgolf.com).

EZLinks sits behind Cloudflare. From a datacenter IP the plain HTTP client is
stopped at the challenge, but a real headless Chromium clears the *managed* JS
challenge on its own (no interactive "verify you are human" step): after a few
seconds the page's own origin can call /api/search/init + /api/search/search and
get full JSON back. Verified on GitHub's runner — cityofaurora returned 373
slots, pinecreekpp 144 — same legitimate technique used for cps.golf. So we run
that exact init+search flow inside a real Chromium and reuse EZLinksAdapter's
parsing (raw_to_slots + course_teetimes) to emit an aggregate-format document.

This owns ALL ezlinks courses (the plain scraper excludes the platform), so the
two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_ezlinks --date 2026-07-24 --out output/ez.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

from .adapters.base import USER_AGENT
from .adapters.ezlinks import EZLinksAdapter
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# init + search, run inside the page (real browser TLS + portal origin). Returns
# the raw r06 rows; EZLinksAdapter.raw_to_slots maps them downstream.
FLOW_JS = r"""
async ([dateMdy]) => {
  const base = location.origin;
  let init;
  try { init = await (await fetch(base + "/api/search/init")).json(); }
  catch (e) { return {stage:"init", error:String(e).slice(0,80)}; }
  const ids = String(init.AllCourseIDs || "").split(",")
      .filter(x => x.trim()).map(Number);
  if (!ids.length) return {stage:"init", error:"no course ids (challenge not cleared)"};
  const body = {p01:ids, p02:dateMdy, p03:"5:00 AM", p04:"7:00 PM",
                p05:0, p06:2, p07:false};
  const s = await fetch(base + "/api/search/search", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  let rows = []; try { rows = (await s.json()).r06 || []; } catch (e) {}
  return {stage:"search", status:s.status, rows};
}
"""


def run(date: dt.date, registry_path: str, out_path: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry
               if c["platform"] == "ezlinks" and c["ids"].get("portal")]
    courses = apply_shard(courses, shard)
    # group registry courses by portal — one portal serves several courses
    portals: dict[str, list[dict]] = {}
    for c in courses:
        portals.setdefault(c["ids"]["portal"], []).append(c)
    date_mdy = date.strftime("%m/%d/%Y")
    log.info("browser-fetching %d ezlinks portals (%d courses) for %s",
             len(portals), len(courses), date)

    tee_times, errors = [], []
    ok_slugs: set[str] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        for i, (portal, pcourses) in enumerate(portals.items()):
            if i:
                page.wait_for_timeout(1500)     # pace between portals
            last = None
            rows = None
            for attempt in range(3):            # give the managed challenge time
                try:
                    page.goto(f"https://{portal}.ezlinksgolf.com/index.html#!/search",
                              wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(7000)  # let Cloudflare's JS auto-clear
                    r = page.evaluate(FLOW_JS, [date_mdy])
                    last = f"{r.get('stage')} {r.get('status') or r.get('error')}"
                    if r.get("stage") == "search" and r.get("status") == 200:
                        rows = r.get("rows") or []
                        break
                except Exception as e:  # noqa: BLE001
                    last = f"{type(e).__name__}"
                page.wait_for_timeout(3000 * (attempt + 1))
            if rows is None:
                for c in pcourses:
                    errors.append({"course": c["slug"], "platform": "ezlinks",
                                   "error": f"browser {last}"})
                log.info("  portal %-18s ERROR %s", portal, last)
                continue
            slots = EZLinksAdapter.raw_to_slots(rows)
            for c in pcourses:
                tts = EZLinksAdapter.course_teetimes(c, slots)
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
    p = argparse.ArgumentParser(description="Browser-based EZLinks fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/ez.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
