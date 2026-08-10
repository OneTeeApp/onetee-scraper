"""Headless-browser fetcher for Club Caddie public booking widgets.

Club Caddie's widget (apimanager-<shard>.clubcaddie.com/webapi/view/<token>) is
a client-rendered SPA. As of Aug 2026 it NO LONGER server-renders an HTML tee
sheet: the old `POST /webapi/TeeTimes` that returned repeated
`Tee Time: </span><br> 06:30 AM ...` blocks now just returns the 49 KB app
shell, so the previous capture-the-POST-and-parse-HTML approach silently parsed
ZERO rows for every course (green run, no data — the nationwide "clubcaddie
mostly dark" gap). Confirmed live 2026-08-10.

CURRENT FLOW (verified live against Applewood cc11/hbfdabab, 2026-08-10):
  1. GET /webapi/view/<token>              — establishes the session (Interaction).
  2. GET /webapi/view/<token>/slots?date=MM/DD/YYYY&player=1&ratetype=any
       — the SPA auto-attaches &Interaction=<id> and CLIENT-SIDE renders the
         tee sheet as repeated `div.teetime` cards:
           "Golfers: 1 - 2  <course> - Front  06:30 AM  $58.00  18 Holes  Book Now"
So we drive a real Chromium: load the widget once (session), then navigate to
the per-date /slots URL, wait for the `div.teetime` cards to render, and read
time / price / holes / golfer-range straight off the rendered DOM. No login, no
CAPTCHA, no challenge — the same public page a golfer uses. Parsing the RENDERED
DOM (not a raw HTML fragment) is resilient to their internal data format.

This owns ALL clubcaddie courses (the plain scraper excludes the platform), so
the two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_clubcaddie --date 2026-07-25 --out-dir output
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
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# Read the rendered tee-time cards off the SPA DOM. Each slot is a `div.teetime`
# card; nested duplicates (card-box-outer > slotBox-body > teetime) are dropped
# by keeping only cards that are not themselves inside another `div.teetime`.
# `shell` proves the booking app actually rendered, so an empty `cards` list can
# be trusted as "no availability" rather than "failed to load".
EXTRACT_JS = r"""
() => {
  const shell = document.title.indexOf('Club Caddie') !== -1
             || !!document.querySelector('.SliderValue, div.teetime');
  const cards = [...document.querySelectorAll('div.teetime')]
    .filter(el => !(el.parentElement && el.parentElement.closest('div.teetime')));
  const out = [];
  for (const el of cards) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const tm = (t.match(/(\d{1,2}:\d\d\s*[AP]M)/i) || [])[1];
    if (!tm) continue;
    const pr = (t.match(/\$([\d,]+(?:\.\d{2})?)/) || [])[1];
    const ho = (t.match(/(\d+)\s*Holes/i) || [])[1];
    const gg = t.match(/Golfers:\s*(\d+)(?:\s*-\s*(\d+))?/i);
    out.push({time: tm, price: pr || null, holes: ho || null,
              gmax: gg ? (gg[2] || gg[1]) : null});
  }
  return {shell, cards: out};
}
"""


def _parse_cards(course: dict, date: dt.date, cards: list) -> list:
    """Turn the extracted card dicts into TeeTime rows, deduped by tee time."""
    by_time: dict[str, dict] = {}
    for c in cards:
        raw = (c.get("time") or "").upper().replace(" ", "")
        try:
            t = dt.datetime.strptime(raw, "%I:%M%p").time()
        except ValueError:
            continue
        iso = dt.datetime.combine(date, t).isoformat()
        e = by_time.setdefault(iso, {"holes": set(), "prices": [], "spots": 0})
        try:
            h = int(c.get("holes")) if c.get("holes") else None
        except (TypeError, ValueError):
            h = None
        if h in (9, 18):
            e["holes"].add(h)
        if c.get("price"):
            try:
                p = float(str(c["price"]).replace(",", ""))
                if p > 0:
                    e["prices"].append(p)
            except ValueError:
                pass
        # "Golfers: 1 - 4" => up to 4 bookable = 4 open spots. Take the max
        # across cards sharing the tee time (front/back, rate types).
        try:
            g = int(c.get("gmax")) if c.get("gmax") else 0
        except (TypeError, ValueError):
            g = 0
        e["spots"] = max(e["spots"], g)

    out = []
    for iso, e in by_time.items():
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=iso, holes=sorted(e["holes"]) or [18],
            open_spots=(e["spots"] or None),
            price_min=min(e["prices"]) if e["prices"] else None,
            price_max=max(e["prices"]) if e["prices"] else None,
            raw={}))
    return out


def _fetch_course(pw, course: dict, dates: list[dt.date]) -> tuple[dict, str | None]:
    """Render the widget per date and read the tee-time cards off the DOM.

    Returns {date_iso: [TeeTime] | None}. None marks a date whose page failed
    to render (unknown) so sync shields the course's existing D1 rows; [] is a
    trustworthy empty day (the app shell rendered with no slots)."""
    ids = course["ids"]
    base = f"https://apimanager-{ids['shard']}.clubcaddie.com"
    token = ids["view_token"]
    last = None
    for attempt in range(3):
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            # Establish the session (Interaction cookie) once, like a golfer
            # opening the widget, before requesting individual dates.
            page.goto(f"{base}/webapi/view/{token}",
                      wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("div.teetime, .SliderValue", timeout=15000)
            except Exception:  # noqa: BLE001 — shell may still be usable
                pass

            per_date: dict[str, list | None] = {}
            for d in dates:
                mdy = d.strftime("%m/%d/%Y")
                try:
                    page.goto(f"{base}/webapi/view/{token}/slots"
                              f"?date={mdy}&player=1&ratetype=any",
                              wait_until="domcontentloaded", timeout=45000)
                    # cards render client-side after load; wait briefly for the
                    # first one, else confirm the shell (=> genuine empty day).
                    try:
                        page.wait_for_selector("div.teetime", timeout=9000)
                    except Exception:  # noqa: BLE001
                        pass
                    r = page.evaluate(EXTRACT_JS)
                    if not r or not r.get("shell"):
                        per_date[d.isoformat()] = None
                        last = "shell did not render"
                    else:
                        per_date[d.isoformat()] = _parse_cards(
                            course, d, r.get("cards") or [])
                except Exception as e:  # noqa: BLE001
                    per_date[d.isoformat()] = None
                    last = f"nav {type(e).__name__}"
                page.wait_for_timeout(300)

            # success if at least one date rendered (list, incl. empty)
            if any(v is not None for v in per_date.values()):
                return per_date, (last if any(v is None for v in per_date.values())
                                  else None)
            last = last or "no dates rendered"
        except Exception as e:  # noqa: BLE001
            last = last or type(e).__name__
        finally:
            browser.close()
        time.sleep(2 * (attempt + 1))
    return {}, last


def run(dates: list[dt.date], registry_path: str, out_dir: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = [c for c in registry if c["platform"] == "clubcaddie"
               and c["ids"].get("shard") and c["ids"].get("view_token")]
    courses = apply_shard(courses, shard)
    log.info("browser-fetching %d clubcaddie courses for %d dates",
             len(courses), len(dates))

    # one browser session per course serves all dates; accumulate per-date docs
    per_date_times: dict[str, list] = {d.isoformat(): [] for d in dates}
    errors: dict[str, list] = {d.isoformat(): [] for d in dates}
    with sync_playwright() as pw:
        for c in courses:
            got, err = _fetch_course(pw, c, dates)
            total = sum(len(v) for v in got.values() if v)
            if err and not got:
                # nothing captured at all: every requested date is unknown
                for d in dates:
                    errors[d.isoformat()].append(
                        {"course": c["slug"], "platform": "clubcaddie",
                         "error": f"browser {err}"})
                log.info("  %-32s ERROR %s", c["slug"], err)
            else:
                # per-date: a None marks a failed render — error that date so
                # sync shields the course's rows instead of reading "empty".
                failed = 0
                for diso, tts in got.items():
                    if tts is None:
                        failed += 1
                        errors[diso].append(
                            {"course": c["slug"], "platform": "clubcaddie",
                             "error": f"browser {err or 'render failed'}"})
                    else:
                        per_date_times[diso].extend(tts)
                log.info("  %-32s %d times (%d dates, %d failed)", c["slug"],
                         total, sum(1 for v in got.values() if v), failed)

    out_paths = {}
    outp = pathlib.Path(out_dir)
    outp.mkdir(parents=True, exist_ok=True)
    for d in dates:
        diso = d.isoformat()
        doc = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": diso,
            "courses_queried": len(courses),
            "courses_ok": len(courses) - len(errors[diso]),
            "tee_times": [t.to_dict() for t in per_date_times[diso]],
            "errors": errors[diso],
        }
        path = outp / f"cc_{diso}.json"
        path.write_text(json.dumps(doc, indent=2))
        out_paths[diso] = str(path)
        log.info("wrote %s (%d tee times, %d errors)", path,
                 len(per_date_times[diso]), len(errors[diso]))
    return out_paths


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based Club Caddie fetcher")
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
