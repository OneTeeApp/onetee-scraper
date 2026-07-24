"""Headless-browser fetcher for Club Caddie public booking widgets.

Club Caddie's widget (apimanager-<shard>.clubcaddie.com/webapi/view/<token>) is
session-gated: plain HTTP gets a "session expired" stub. But once a real Chromium
loads the widget, the page fires a POST to /webapi/TeeTimes and gets back an HTML
tee sheet. That POST body carries everything we need — CourseId, the public
apikey (view token) and the session Interaction id:

  POST /webapi/TeeTimes
    date=MM/DD/YYYY&player=1&holes=any&fromtime=4&totime=23&minprice=0
    &maxprice=9999&ratetype=any&HoleGroup=front&CourseId=<id>&apikey=<token>
    &Interaction=<sessionId>
    (X-Requested-With: XMLHttpRequest; form-urlencoded)
  -> HTML with repeated blocks:
       Tee Time: <br> 06:30 AM   Price: <br>$58.00   Holes: <br>18

So we load the widget, capture that POST body, then re-POST it in-page with the
date swapped to each target date and parse the HTML. No login, no CAPTCHA, no
challenge-solving — the same public page a golfer uses.

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
import re
import sys
import time

from .adapters.base import USER_AGENT
from .adapters.experimental import GolfNowAdapter  # base_tee_time host
from .aggregate import load_registry

log = logging.getLogger("teetime")

# Re-POST the captured body with the date swapped, in-page (real session).
REPLAY_JS = r"""
async ([body, dateStr]) => {
  const p = new URLSearchParams(body);
  p.set("date", dateStr);
  const r = await fetch("/webapi/TeeTimes", {method: "POST",
    headers: {"X-Requested-With": "XMLHttpRequest",
              "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    body: p.toString()});
  return {status: r.status, html: await r.text()};
}
"""

_TIME = re.compile(r"Tee Time:\s*</span>\s*<br>\s*(\d{1,2}:\d\d\s*[AP]M)", re.I)
_PRICE = re.compile(r"Price:\s*</span>\s*<br>\s*\$?([\d,]+(?:\.\d+)?)", re.I)
_HOLES = re.compile(r"Holes:\s*</span>\s*<br>\s*(\d+)", re.I)


def _parse(course: dict, date: dt.date, html: str) -> list:
    # split into per-slot chunks at each "Tee Time:" marker
    chunks = re.split(r"(?=Tee Time:\s*</span>)", html)
    by_time: dict[str, dict] = {}
    for ch in chunks:
        tm = _TIME.search(ch)
        if not tm:
            continue
        raw = re.sub(r"\s+", " ", tm.group(1)).strip().upper().replace(" ", "")
        try:
            t = dt.datetime.strptime(raw, "%I:%M%p").time()
        except ValueError:
            continue
        iso = dt.datetime.combine(date, t).isoformat()
        pm = _PRICE.search(ch)
        price = float(pm.group(1).replace(",", "")) if pm else None
        hm = _HOLES.search(ch)
        holes = int(hm.group(1)) if hm else None
        e = by_time.setdefault(iso, {"holes": set(), "prices": []})
        if holes in (9, 18):
            e["holes"].add(holes)
        if price and price > 0:
            e["prices"].append(price)

    out = []
    for iso, e in by_time.items():
        # Club Caddie's tee sheet doesn't expose a reliable open-spots count,
        # so we leave open_spots unset rather than publish a wrong number.
        out.append(GolfNowAdapter.base_tee_time(
            course, teetime=iso, holes=sorted(e["holes"]) or [18],
            open_spots=None,
            price_min=min(e["prices"]) if e["prices"] else None,
            price_max=max(e["prices"]) if e["prices"] else None,
            raw={}))
    return out


def _fetch_course(pw, course: dict, dates: list[dt.date]) -> tuple[dict, str | None]:
    """Load the widget once (capture the TeeTimes POST body), then replay for
    each date. Returns {date_iso: [TeeTime]} and an error string if it failed."""
    ids = course["ids"]
    base = f"https://apimanager-{ids['shard']}.clubcaddie.com"
    token = ids["view_token"]
    last = None
    for attempt in range(3):
        browser = pw.chromium.launch(args=["--no-sandbox"])
        captured: dict[str, str] = {}
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()

            def on_req(rq):
                if "/webapi/TeeTimes" in rq.url and rq.method == "POST":
                    try:
                        captured["body"] = rq.post_data or ""
                    except Exception:
                        pass

            page.on("request", on_req)
            page.goto(f"{base}/webapi/view/{token}",
                      wait_until="networkidle", timeout=45000)
            # give the auto-fired TeeTimes POST time to land
            for _ in range(16):
                if captured.get("body"):
                    break
                page.wait_for_timeout(500)
            if not captured.get("body"):
                last = "no TeeTimes POST observed"
                raise RuntimeError(last)
            per_date: dict[str, list] = {}
            for d in dates:
                r = page.evaluate(REPLAY_JS, [captured["body"], d.strftime("%m/%d/%Y")])
                if r.get("status") == 200 and (r.get("html") or "").strip()[:1] == "<":
                    per_date[d.isoformat()] = _parse(course, d, r["html"])
                else:
                    per_date[d.isoformat()] = []
                page.wait_for_timeout(400)
            return per_date, None
        except Exception as e:  # noqa: BLE001
            last = last or type(e).__name__
        finally:
            browser.close()
        time.sleep(2 * (attempt + 1))
    return {}, last


def run(dates: list[dt.date], registry_path: str, out_dir: str) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    courses = [c for c in registry if c["platform"] == "clubcaddie"
               and c["ids"].get("shard") and c["ids"].get("view_token")]
    log.info("browser-fetching %d clubcaddie courses for %d dates",
             len(courses), len(dates))

    # one browser session per course serves all dates; accumulate per-date docs
    per_date_times: dict[str, list] = {d.isoformat(): [] for d in dates}
    errors: dict[str, list] = {d.isoformat(): [] for d in dates}
    with sync_playwright() as pw:
        for c in courses:
            got, err = _fetch_course(pw, c, dates)
            total = sum(len(v) for v in got.values())
            if err and not total:
                for d in dates:
                    errors[d.isoformat()].append(
                        {"course": c["slug"], "platform": "clubcaddie",
                         "error": f"browser {err}"})
                log.info("  %-32s ERROR %s", c["slug"], err)
            else:
                for diso, tts in got.items():
                    per_date_times[diso].extend(tts)
                log.info("  %-32s %d times (%d dates)", c["slug"], total,
                         sum(1 for v in got.values() if v))

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
    p.add_argument("--out-dir", default="output")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    start = dt.date.fromisoformat(a.date)
    dates = [start + dt.timedelta(days=n) for n in range(a.days)]
    run(dates, a.registry, a.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
