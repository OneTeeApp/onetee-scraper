"""Headless-browser fetcher for Club Caddie public booking widgets.

Club Caddie's consumer endpoint (apimanager-<shard>.clubcaddie.com/webapi/view/
<token>) is NOT a JSON API — the tee sheet is rendered server-side into an HTML
page, and the endpoint returns a "PHPSESSID expired" stub to any client that
hasn't gone through the widget's own session handshake (so plain HTTP always
fails). A real Chromium loads the widget, establishes the session, and — after
setting the date filter (#dateinput) and clicking Search (#UpdateFilerButton) —
renders the tee-time cards into the DOM, which we scrape. No login, no CAPTCHA,
no challenge-solving: this is the same public page a golfer sees.

This owns ALL clubcaddie courses (the plain scraper excludes the platform), so
the two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_clubcaddie --date 2026-07-25 --out output/cc.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
import time

from .adapters.base import USER_AGENT
from .adapters.experimental import GolfNowAdapter  # base_tee_time host
from .aggregate import load_registry

log = logging.getLogger("teetime")

# Scrape rendered tee-time cards: each has a time; climb to the container that
# also holds a price, then pull price / holes / open spots from its text.
EXTRACT_JS = r"""
() => {
  const all = [...document.querySelectorAll("*")];
  const timeEls = all.filter(e => e.children.length === 0 &&
      /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent || ""));
  const out = [];
  for (const te of timeEls) {
    let card = te;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      card = card.parentElement;
      if (/\$/.test(card.textContent)) break;
    }
    if (card.__cc_done) continue;
    card.__cc_done = true;
    const ct = card.textContent.replace(/\s+/g, " ");
    const price = (ct.match(/\$\s*([\d]+(?:\.\d+)?)/) || [])[1];
    const holes = (ct.match(/(\d+)\s*hole/i) || [])[1];
    const spots = (ct.match(/(\d+)\s*(?:player|spot|golfer|available)/i) || [])[1];
    out.push({time: te.textContent.trim(),
              price: price ? parseFloat(price) : null,
              holes: holes ? parseInt(holes) : null,
              spots: spots ? parseInt(spots) : null});
  }
  return out;
}
"""


def _to_teetimes(course: dict, date: dt.date, cards: list[dict]) -> list:
    out = []
    seen = set()
    for c in cards:
        raw = (c.get("time") or "").strip()
        if not raw:
            continue
        try:
            t = dt.datetime.strptime(raw.upper().replace(" ", ""), "%I:%M%p").time()
        except ValueError:
            continue
        iso = dt.datetime.combine(date, t).isoformat()
        if iso in seen:
            continue
        seen.add(iso)
        price = c.get("price")
        holes = [c["holes"]] if c.get("holes") in (9, 18) else [18]
        spots = c.get("spots")
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=iso, holes=holes,
            open_spots=spots if isinstance(spots, int) else None,
            price_min=price, price_max=price, raw={}))
    return out


def _fetch_course(pw, course: dict, date: dt.date) -> tuple[list, str | None]:
    ids = course["ids"]
    base = f"https://apimanager-{ids['shard']}.clubcaddie.com"
    token = ids["view_token"]
    mdy = date.strftime("%m/%d/%Y")
    last = None
    for attempt in range(3):
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            page.goto(f"{base}/webapi/view/{token}",
                      wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(4000)   # establish session + initial render
            # set the date filter and search
            try:
                page.fill("#dateinput", mdy, timeout=8000)
                page.eval_on_selector(
                    "#dateinput",
                    "el => el.dispatchEvent(new Event('change', {bubbles:true}))")
                page.click("#UpdateFilerButton", timeout=8000)
            except Exception:  # noqa: BLE001 — fall back to whatever auto-rendered
                pass
            page.wait_for_timeout(5000)   # let results render
            cards = page.evaluate(EXTRACT_JS)
            if cards:
                return _to_teetimes(course, date, cards), None
            last = "no cards rendered"
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        finally:
            browser.close()
        time.sleep(2 * (attempt + 1))
    return [], last


def run(date: dt.date, registry_path: str, out_path: str) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    courses = [c for c in registry if c["platform"] == "clubcaddie"
               and c["ids"].get("shard") and c["ids"].get("view_token")]
    log.info("browser-fetching %d clubcaddie courses for %s", len(courses), date)

    tee_times, errors = [], []
    with sync_playwright() as pw:
        for c in courses:
            tts, err = _fetch_course(pw, c, date)
            if err and not tts:
                errors.append({"course": c["slug"], "platform": "clubcaddie",
                               "error": f"browser {err}"})
                log.info("  %-34s ERROR %s", c["slug"], err)
            else:
                tee_times.extend(tts)
                log.info("  %-34s %d times", c["slug"], len(tts))

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
    p = argparse.ArgumentParser(description="Browser-based Club Caddie fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--out", default="output/cc.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
