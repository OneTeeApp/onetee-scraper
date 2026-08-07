"""Headless-browser fetcher for TenFore (fox.tenfore.golf / swan.tenfore.golf).

TenFore's priced, bookable tee-time endpoint (/api/TeeTimes/Search) is gated by
reCAPTCHA Enterprise: every request needs a fresh token in the header
`x-recaptcha-token`, minted by grecaptcha.enterprise.execute() in a real browser.
So we load ONE TenFore page (any vanity — the API + site key are host-global on
fox.tenfore.golf), wait for grecaptcha, then mint a token and call the JSON API
IN-PAGE for every course x date x holes. One Chromium session covers the whole
TenFore fleet — no per-course navigation.

See scraper/adapters/tenfore.py for the endpoint/param details and the parser.

Usage:
    # multi-date (one session, one doc per date):
    python -m scraper.browser_tenfore --dates 2026-08-07,2026-08-08 --out-dir output
    # single date:
    python -m scraper.browser_tenfore --date 2026-08-07 --out output/tenfore.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys

from .adapters.base import USER_AGENT
from .adapters.tenfore import (TenForeAdapter, API_BASE, SITE_KEY, APP_ID,
                               RECAPTCHA_ACTION, HOLES)
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# In-page: mint a reCAPTCHA token, then GET /api/TeeTimes/Search for one
# course+date+holes. Returns the parsed JSON array (or an error marker).
SEARCH_JS = r"""
async ({gid, dFrom, dTo, players, holes, siteKey, action, appid}) => {
  const g = (window.grecaptcha && (window.grecaptcha.enterprise || window.grecaptcha));
  if (!g || !g.execute) return {ok:false, err:"grecaptcha-missing"};
  let token;
  try { token = await g.execute(siteKey, {action}); }
  catch (e) { return {ok:false, err:"token:"+String(e).slice(0,60)}; }
  const u = "https://swan.tenfore.golf/api/TeeTimes/Search"
      + `?golfCourseIds=${gid}&dateFrom=${dFrom}&dateTo=${dTo}`
      + `&players=${players}&holes=${holes}`;
  try {
    const r = await fetch(u, {headers: {
      "x-recaptcha-action": action,
      "x-recaptcha-token": token,
      "x-tenfore-appid": appid,
      "accept": "application/json",
    }});
    const t = await r.text();
    try { return {ok:true, status:r.status, data: JSON.parse(t)}; }
    catch (e) { return {ok:false, status:r.status, body: t.slice(0,150)}; }
  } catch (e) { return {ok:false, err:"fetch:"+String(e).slice(0,60)}; }
}
"""


# --- anti-detection --------------------------------------------------------
# TenFore's priced endpoint is reCAPTCHA Enterprise (score-based). Default
# headless Chromium scores low (navigator.webdriver=true, automation flags,
# HeadlessChrome UA) and tokens get rejected -> empty results. These flags +
# init script remove the obvious "I am a bot" tells so the score clears.
_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]
_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def _launch_kwargs() -> dict:
    """chromium.launch kwargs. Routes through a residential proxy if TENFORE_PROXY
    (or TEEITUP_PROXY as a fallback) is set — a residential exit scores much higher
    on reCAPTCHA than a GitHub datacenter IP. No proxy by default (free path)."""
    import re
    kwargs = {"args": list(_STEALTH_ARGS)}
    raw = re.sub(r"\s+", "", os.environ.get("TENFORE_PROXY", "")
                 or os.environ.get("TEEITUP_PROXY", ""))
    if raw:
        import urllib.parse
        if "://" not in raw:
            raw = "http://" + raw
        pu = urllib.parse.urlparse(raw)
        server = f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")
        proxy = {"server": server}
        if pu.username:
            proxy["username"] = urllib.parse.unquote(pu.username)
        if pu.password:
            proxy["password"] = urllib.parse.unquote(pu.password)
        kwargs["proxy"] = proxy
        log.info("tenfore: routing Chromium through proxy %s", server)
    # reCAPTCHA Enterprise's strongest signal is headless mode itself — headless
    # Chromium never got grecaptcha to execute (both datacenter + residential
    # proxy runs crashed at the grecaptcha-ready wait). Run REAL headful Chromium
    # when a display is available (the CI step runs us under xvfb-run, which sets
    # DISPLAY); fall back to headless only if there is no display.
    kwargs["headless"] = not bool(os.environ.get("DISPLAY"))
    log.info("tenfore: launching Chromium headless=%s (DISPLAY=%s)",
             kwargs["headless"], os.environ.get("DISPLAY") or "unset")
    return kwargs


def _select_courses(registry: list, shard: str | None) -> list:
    courses = [c for c in registry
               if c["platform"] == "tenfore"
               and (c.get("ids") or {}).get("golf_course_id")]
    return apply_shard(courses, shard)


def _search(page, gid: str, date: dt.date, holes: int) -> tuple[list | None, str | None]:
    """One Search call (with a fresh token). Returns (rows, error)."""
    arg = {"gid": str(gid),
           "dFrom": f"{date.isoformat()}T00:00:00",
           "dTo": f"{date.isoformat()}T23:59:59",
           "players": 1, "holes": holes,
           "siteKey": SITE_KEY, "action": RECAPTCHA_ACTION, "appid": APP_ID}
    for attempt in range(2):
        try:
            res = page.evaluate(SEARCH_JS, arg)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "err": "evaluate:" + type(e).__name__}
        if res.get("ok") and isinstance(res.get("data"), list):
            return res["data"], None
        # non-2xx or challenge body -> retry once (fresh token), else report
        err = res.get("err") or f"http {res.get('status')}: {str(res.get('body'))[:80]}"
        if attempt == 0:
            page.wait_for_timeout(1200)
            continue
        return None, err
    return None, "unknown"


def run(dates: list[dt.date], registry_path: str,
        out_paths_by_iso: dict[str, str], shard: str | None = None) -> list[str]:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    courses = _select_courses(registry, shard)
    log.info("tenfore: %d courses x %d dates (one browser session)",
             len(courses), len(dates))

    # date -> {slug: [TeeTime]}, and date -> errors[]
    per_date_tt: dict[str, list] = {d.isoformat(): [] for d in dates}
    per_date_err: dict[str, list] = {d.isoformat(): [] for d in dates}
    written: list[str] = []

    if courses:
        boot_vanity = (courses[0].get("ids") or {}).get("vanity") or "needwood"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**_launch_kwargs())
            try:
                # a realistic context scores far better on reCAPTCHA Enterprise
                # than a bare headless page
                ctx = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    timezone_id="America/New_York",
                    viewport={"width": 1366, "height": 850},
                )
                ctx.add_init_script(_STEALTH_JS)
                page = ctx.new_page()
                page.goto(f"https://fox.tenfore.golf/{boot_vanity}",
                          wait_until="domcontentloaded", timeout=45000)
                # wait for grecaptcha enterprise to be executable
                page.wait_for_function(
                    "() => window.grecaptcha && "
                    "(window.grecaptcha.enterprise||window.grecaptcha).execute",
                    timeout=45000)
                # human-ish warmup: a mouse move + settle lifts the reCAPTCHA score
                try:
                    page.mouse.move(400, 300)
                    page.mouse.move(700, 450)
                    page.mouse.wheel(0, 600)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(3000)

                for c in courses:
                    gid = (c["ids"] or {}).get("golf_course_id")
                    for d in dates:
                        iso = d.isoformat()
                        rows_by_holes: dict[int, list] = {}
                        errs = []
                        for h in HOLES:
                            rows, err = _search(page, gid, d, h)
                            if rows is not None:
                                rows_by_holes[h] = rows
                            elif err:
                                errs.append(f"h{h}:{err}")
                            page.wait_for_timeout(300)  # polite pacing
                        if rows_by_holes:
                            tts = TenForeAdapter.rows_to_teetimes(c, d, rows_by_holes)
                            per_date_tt[iso].extend(tts)
                            log.info("  %-34s %s  %d slots", c["slug"], iso, len(tts))
                        else:
                            # every holes query failed -> a real error for this
                            # (course,date); record so sync shields existing rows
                            per_date_err[iso].append(
                                {"course": c["slug"], "platform": "tenfore",
                                 "error": "; ".join(errs) or "no data"})
                            log.info("  %-34s %s  ERROR %s", c["slug"], iso,
                                     "; ".join(errs)[:80])
            finally:
                browser.close()

    for d in dates:
        iso = d.isoformat()
        tee_times = per_date_tt[iso]
        errors = per_date_err[iso]
        ok = len(courses) - len({e["course"] for e in errors})
        doc = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": iso,
            "courses_queried": len(courses),
            "courses_ok": ok,
            "tee_times": [t.to_dict() for t in tee_times],
            "errors": errors,
        }
        out = pathlib.Path(out_paths_by_iso[iso])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2))
        written.append(str(out))
        log.info("wrote %s (%d tee times, %d errors)", out, len(tee_times), len(errors))
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based TenFore fetcher")
    p.add_argument("--dates", help="comma-separated ISO dates -> --out-dir mode")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/tenfore.json")
    p.add_argument("--out-dir", default="output")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    if a.dates:
        dates = [dt.date.fromisoformat(s.strip()) for s in a.dates.split(",") if s.strip()]
        out_paths = {d.isoformat(): str(pathlib.Path(a.out_dir) / f"tenfore_{d.isoformat()}.json")
                     for d in dates}
        run(dates, a.registry, out_paths, a.shard)
    else:
        d = dt.date.fromisoformat(a.date)
        run([d], a.registry, {d.isoformat(): a.out}, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
