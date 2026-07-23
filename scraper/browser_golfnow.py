"""Headless-browser fetcher for GolfNow facilities (golfnow.com).

GolfNow shows no interactive challenge to a real browser (verified: the facility
search page loads and fires its own tee-time API from a datacenter IP), but the
plain HTTP client is blocked. So we load each facility's search page in a real
Chromium, capture the predicate body the page itself POSTs to
    /api/tee-times/tee-time-search-results  ->  {ttResults:{teeTimes:[...]}}
and replay it in-page with the date swapped forward. Capturing the page's own
body means we inherit its correct lat/long/radius per facility instead of
guessing. Results are filtered to the target facilityId so a nearby course in
the radius can't leak in.

This owns ALL golfnow courses (the plain scraper excludes the platform), so the
two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_golfnow --date 2026-07-24 --out output/gn.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

from .adapters.base import USER_AGENT
from .adapters.experimental import GolfNowAdapter
from .aggregate import load_registry

log = logging.getLogger("teetime")

SEARCH_EP = "/api/tee-times/tee-time-search-results"

# Replay the page's own predicate for the requested date, in-page (real browser
# TLS + golfnow origin). Returns slim slots filtered to the target facility.
FETCH_JS = r"""
async ([bodyStr, dateStr, fid]) => {
  let body;
  try { body = JSON.parse(bodyStr); } catch (e) { return {error: "bad body"}; }
  body.date = dateStr; body.pageSize = 40; body.teeTimeCount = 40; body.pageNumber = 0;
  const r = await fetch(location.origin + "/api/tee-times/tee-time-search-results",
    {method:"POST", headers:{"Content-Type":"application/json","Accept":"application/json"},
     body: JSON.stringify(body)});
  let j = {}; try { j = await r.json(); } catch (e) {}
  const tt = (j.ttResults && j.ttResults.teeTimes) || [];
  const money = (m) => (m && typeof m.value === "number") ? m.value : null;
  const out = [];
  for (const s of tt) {
    if (fid && s.facilityId !== fid) continue;
    out.push({
      date: s.time && s.time.date,
      playerRule: s.playerRule,
      detailUrl: s.detailUrl,
      display: money(s.displayRate),
      minRate: money(s.minTeeTimeRate),
      maxRate: money(s.maxTeeTimeRate),
      rates: (s.teeTimeRates || []).map((x) => ({
        holes: x.holeCount,
        greens: x.singlePlayerPrice ? money(x.singlePlayerPrice.greensFees) : null,
      })),
    });
  }
  return {status: r.status, slots: out};
}
"""


def _spots(rule: str | None) -> int | None:
    """GolfNow's playerRule ('Two', 'TwoThreeFour', ...) lists the allowed
    party sizes; the largest word is the max players that can still book."""
    if not rule:
        return None
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    r = rule.lower()
    best = max((n for w, n in words.items() if w in r), default=0)
    return best or None


def _slots_to_teetimes(course: dict, slots: list[dict]) -> list:
    by_time: dict[str, dict] = {}
    for s in slots:
        d = s.get("date")
        if not d:
            continue
        t = str(d)[:19]  # GolfNow labels local time with a +00:00 offset — the
        holes = sorted({r["holes"] for r in (s.get("rates") or [])  # datetime part
                        if r.get("holes")}) or [18]                 # is local
        prices = [r["greens"] for r in (s.get("rates") or []) if r.get("greens")]
        if s.get("display"):
            prices.append(s["display"])
        prices = [p for p in prices if p and p > 0]
        e = by_time.setdefault(t, {"holes": set(), "prices": [], "spots": 0})
        e["holes"].update(holes)
        e["prices"].extend(prices)
        e["spots"] = max(e["spots"], _spots(s.get("playerRule")) or 0)
    out = []
    for t, e in by_time.items():
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=t, holes=sorted(e["holes"]),
            open_spots=e["spots"] or None,
            price_min=min(e["prices"]) if e["prices"] else None,
            price_max=max(e["prices"]) if e["prices"] else None,
            raw={}))
    return out


def run(date: dt.date, registry_path: str, out_path: str) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    courses = [c for c in registry
               if c["platform"] == "golfnow" and c["ids"].get("golfnow_facility_id")]
    date_str = f"{date:%b} {date.day} {date:%Y}"   # "Jul 5 2026" (no zero-pad)
    log.info("browser-fetching %d golfnow facilities for %s", len(courses), date)

    tee_times, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        captured: dict[str, str] = {}

        def on_request(req):
            if SEARCH_EP in req.url and req.method == "POST":
                try:
                    captured["body"] = req.post_data or ""
                except Exception:
                    pass

        page.on("request", on_request)

        for i, c in enumerate(courses):
            fid = int(c["ids"]["golfnow_facility_id"])
            # GolfNow 404s the facility page when the slug segment doesn't match
            # its canonical one (learned via Black Bear), so prefer the slug
            # captured from the booking URL over our registry slug.
            gn_slug = c["ids"].get("golfnow_slug") or c["slug"]
            if i:
                page.wait_for_timeout(1200)
            last = None
            got = False
            for attempt in range(3):
                try:
                    captured.pop("body", None)
                    page.goto(
                        f"https://www.golfnow.com/tee-times/facility/{fid}-{gn_slug}/search",
                        wait_until="domcontentloaded", timeout=45000)
                    # wait (up to ~12s) for the page to POST its own predicate
                    for _ in range(24):
                        page.wait_for_timeout(500)
                        if captured.get("body"):
                            break
                    if not captured.get("body"):
                        last = "no search body captured"
                        raise RuntimeError(last)
                    r = page.evaluate(FETCH_JS, [captured["body"], date_str, fid])
                    last = f"status {r.get('status')}"
                    if r.get("status") == 200:
                        tts = _slots_to_teetimes(c, r.get("slots") or [])
                        tee_times.extend(tts)
                        log.info("  %-34s %d times", c["slug"], len(tts))
                        got = True
                        break
                except Exception as e:  # noqa: BLE001
                    last = last or f"{type(e).__name__}"
                page.wait_for_timeout(2500 * (attempt + 1))
            if not got:
                errors.append({"course": c["slug"], "platform": "golfnow",
                               "error": f"browser {last}"})
                log.info("  %-34s ERROR %s", c["slug"], last)
        browser.close()

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
    p = argparse.ArgumentParser(description="Browser-based GolfNow fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--out", default="output/gn.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
