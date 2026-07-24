"""Headless-browser fetcher for SuperSaaS public schedules.

Spreading Antlers publishes its tee sheet as a public SuperSaaS schedule. The
day view is JS-rendered (the plain HTML has no slots), so a real Chromium loads
    /schedule/<acct>/<sched>?year=Y&month=M&day=D&view=day
and the open tee slots appear as clickable "new reservation" chips (class ~nr).
Booked/blocked cells carry `out`/`f`. We scrape the open chips per date. Login
is only needed to *book*, not to view availability.

This owns ALL supersaas courses (excluded from the plain scraper), so the two
never write the same course_slug.

Usage:
    python -m scraper.browser_supersaas --date 2026-07-25 --out output/ss.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import re
import sys
import time
import urllib.parse

from .adapters.base import USER_AGENT
from .adapters.experimental import GolfNowAdapter  # base_tee_time host
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# Collect open ("nr") tee-slot chips: return each chip's time text.
EXTRACT_JS = r"""
() => {
  const out = [];
  for (const e of document.querySelectorAll('[class~="nr"]')) {
    const cls = " " + (e.className || "") + " ";
    if (cls.includes(" out ") || cls.includes(" f ")) continue;  // booked/blocked
    const t = (e.textContent || "").trim();
    if (/^\d?\d:\d\d\s*[ap]m/i.test(t)) out.push(t.replace(/\s+/g, " ").slice(0, 30));
  }
  return out;
}
"""
_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)", re.I)


def _to_teetimes(course: dict, date: dt.date, chips: list[str]) -> list:
    seen, out = set(), []
    for txt in chips:
        m = _TIME.search(txt)
        if not m:
            continue
        hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if ap == "pm" and hh != 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        iso = dt.datetime.combine(date, dt.time(hh, mm)).isoformat()
        if iso in seen:
            continue
        seen.add(iso)
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=iso, holes=[], open_spots=None,
            price_min=None, price_max=None, raw={}))
    return out


def _fetch_course(pw, course: dict, date: dt.date) -> tuple[list, str | None]:
    ids = course["ids"]
    acct, sched = ids.get("account"), ids.get("schedule")
    if not (acct and sched):
        return [], "missing account/schedule"
    acct_q = urllib.parse.quote(acct, safe="")
    sched_q = urllib.parse.quote(sched, safe="")
    url = (f"https://www.supersaas.com/schedule/{acct_q}/{sched_q}"
           f"?year={date.year}&month={date.month}&day={date.day}&view=day")
    last = None
    for attempt in range(3):
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(6000)   # let the schedule grid render
            chips = page.evaluate(EXTRACT_JS)
            return _to_teetimes(course, date, chips), None
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        finally:
            browser.close()
        time.sleep(2 * (attempt + 1))
    return [], last


def run(date: dt.date, registry_path: str, out_path: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry if c["platform"] == "supersaas"
               and c["ids"].get("account") and c["ids"].get("schedule")]
    courses = apply_shard(courses, shard)
    log.info("browser-fetching %d supersaas courses for %s", len(courses), date)

    tee_times, errors = [], []
    with sync_playwright() as pw:
        for c in courses:
            tts, err = _fetch_course(pw, c, date)
            if err and not tts:
                errors.append({"course": c["slug"], "platform": "supersaas",
                               "error": f"browser {err}"})
                log.info("  %-32s ERROR %s", c["slug"], err)
            else:
                tee_times.extend(tts)
                log.info("  %-32s %d open slots", c["slug"], len(tts))

    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "courses_queried": len(courses),
        "courses_ok": len(courses) - len(errors),
        "tee_times": [t.to_dict() for t in tee_times],
        "errors": errors,
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based SuperSaaS fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/ss.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
