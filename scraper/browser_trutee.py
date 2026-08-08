"""Headless-browser fetcher for Trutee (trutee.app) public tee sheets.

Trutee is a Next.js App-Router (RSC) app: the tee-time data is not in any JSON
API, not in the SSR HTML/`__next_f`, and not in a capturable XHR — it renders
into the DOM only after client hydration + an internal server-component fetch.
So a real Chromium loads the ORG portal for a date and scrapes the rendered
cards:

    https://trutee.app/courses/o/<org>?date=YYYY-MM-DD

The org portal lists every course under that org on one page, one card per open
slot. Each card carries its course in the `<img alt="<Course> image">`, plus the
time, a tee/sub-course tag (Front / Back / Pointe / Woodbridge …), the bookable
party size ("2 - 4 players"), and two prices ($lo $hi). We load each org ONCE
per date and distribute its cards to the registry venues by course name.

Registry (all four City of St. George venues share one org):
    ids = {"org": "city-of-st-george", "trutee_course": "<exact img-alt name>"}
The img-alt name is pinned per venue and matched EXACTLY (no fuzzy matching:
Trutee calls our "Dixie Red Hills Golf Course" simply "Red Hills Golf Course",
and a normaliser that stripped words to bridge that would just as happily merge
two real courses). Sunbrook's sub-courses (Pointe / Woodbridge) all carry the
img-alt "Sunbrook Golf Club", so they map to the one Sunbrook venue and the
sub-course is kept as course_label to stop same-time rows colliding in D1.

This owns ALL trutee courses (the plain scraper --excludes trutee), so the two
never write the same course_slug.

Usage:
    python -m scraper.browser_trutee --date 2026-08-10 --out output/trutee.json
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

from .adapters.base import USER_AGENT
from .adapters.experimental import GolfNowAdapter  # base_tee_time host
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# One card = the smallest ancestor of a time element that also carries a price
# and the course <img>. Returns {course, time, playersMax, label, prices}.
EXTRACT_JS = r"""
() => {
  const out = [];
  const timeEls = [...document.querySelectorAll('*')].filter(
    e => e.children.length === 0 &&
         /^\d{1,2}:\d{2}\s*[AP]M$/i.test((e.textContent || '').trim()));
  const seen = new Set();
  for (const te of timeEls) {
    let card = te;
    for (let i = 0; i < 7 && card; i++) {
      if (/\$\d/.test(card.textContent || '') &&
          (card.textContent || '').length < 300 &&
          card.querySelector('img')) break;
      card = card.parentElement;
    }
    if (!card || seen.has(card)) continue;
    seen.add(card);
    const img = card.querySelector('img[alt]');
    const course = img ? (img.getAttribute('alt') || '').replace(/\s+image$/i, '').trim() : '';
    const txt = (card.textContent || '').replace(/\s+/g, ' ').trim();
    const time = (txt.match(/\d{1,2}:\d{2}\s*[AP]M/i) || [''])[0];
    const pl = txt.match(/(\d+)\s*(?:-\s*(\d+))?\s*players?/i);
    const prices = [...txt.matchAll(/\$(\d+(?:\.\d+)?)/g)].map(m => parseFloat(m[1]));
    const label = (txt.match(/\b(Front|Back|Pointe|Woodbridge|Executive|Championship)\b/i) || [''])[0];
    out.push({ course, time, playersMax: pl ? parseInt(pl[2] || pl[1], 10) : null,
               label, prices });
  }
  return out;
}
"""

# The page has finished loading a date once it either shows the empty-state text
# or has rendered at least one priced time card.
WAIT_JS = r"""
() => {
  const t = document.body.innerText || '';
  if (/no tee times/i.test(t)) return true;
  return /\d{1,2}:\d{2}\s*[AP]M/.test(t) && /\$\d/.test(t);
}
"""

_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)


def _iso(date: dt.date, time_s: str) -> str | None:
    m = _TIME.search(time_s or "")
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "p" and hh != 12:
        hh += 12
    if ap == "a" and hh == 12:
        hh = 0
    return dt.datetime.combine(date, dt.time(hh, mm)).isoformat(timespec="seconds")


def _venue_teetimes(course: dict, date: dt.date, rows: list[dict]) -> list:
    """TeeTimes for the venue whose pinned trutee_course matches the row course."""
    want = (course["ids"].get("trutee_course") or "").strip().lower()
    out = []
    for r in rows:
        if (r.get("course") or "").strip().lower() != want:
            continue
        iso = _iso(date, r.get("time") or "")
        if iso is None:
            continue
        prices = [p for p in (r.get("prices") or []) if isinstance(p, (int, float)) and p > 0]
        spots = r.get("playersMax")
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=iso,
            holes=[],                                # Front/Back is a tee, not hole count
            open_spots=int(spots) if isinstance(spots, int) else None,
            price_min=min(prices) if prices else None,
            price_max=max(prices) if prices else None,
            course_label=(r.get("label") or ""),
            raw=r))
    return out


def _fetch_org(pw, org: str, date: dt.date) -> tuple[list[dict], str | None]:
    url = f"https://trutee.app/courses/o/{org}?date={date.isoformat()}"
    last = None
    for attempt in range(3):
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_function(WAIT_JS, timeout=25000)
            page.wait_for_timeout(1500)              # settle after hydration
            return page.evaluate(EXTRACT_JS), None
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
    venues = [c for c in registry if c["platform"] == "trutee"
              and c["ids"].get("org") and c["ids"].get("trutee_course")]
    venues = apply_shard(venues, shard)

    # Group by org so each org portal is loaded ONCE per date, not once per venue.
    by_org: dict[str, list[dict]] = {}
    for c in venues:
        by_org.setdefault(c["ids"]["org"], []).append(c)
    log.info("browser-fetching %d trutee venues across %d org(s) for %s",
             len(venues), len(by_org), date)

    tee_times, errors = [], []
    with sync_playwright() as pw:
        for org, org_venues in by_org.items():
            rows, err = _fetch_org(pw, org, date)
            if err and not rows:
                # The whole org portal failed to load: mark every venue on it
                # errored so sync shields their existing rows from deactivation.
                for c in org_venues:
                    errors.append({"course": c["slug"], "platform": "trutee",
                                   "error": f"browser {err}"})
                    log.info("  %-34s ERROR %s", c["slug"], err)
                continue
            for c in org_venues:
                tts = _venue_teetimes(c, date, rows)
                tee_times.extend(tts)
                log.info("  %-34s %d slots", c["slug"], len(tts))

    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "courses_queried": len(venues),
        "courses_ok": len(venues) - len(errors),
        "tee_times": [t.to_dict() for t in tee_times],
        "errors": errors,
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based Trutee fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/trutee.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
