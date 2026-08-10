"""PROBE: does kenna (TeeItUp backend) serve a PATCHRIGHT browser from a
DATACENTER IP with NO proxy — or is TeeItUp's block IP/rate-limit based (so a
stealth browser alone can't help)?

Unlike cps.golf (a Cloudflare managed challenge that keys on automation detection,
which Patchright defeats), browser_teeitup.py documents kenna as blocking the
datacenter runner at the network/rate-limit layer: "Failed to fetch" on ~87
courses a pass + 429 on the plain client, while residential succeeds. That reads
like an IP block, which Patchright (a non-leaky browser, same IP) would NOT fix.
This probe settles it empirically: load a *.book.teeitup.com origin in Patchright
(no proxy) and fire kenna's facilities + tee-times (x-be-alias header) for several
aliases at a FAR date (where the throttle bit hardest). Real slots => Patchright is
enough; 429 / throttle-parse-fail => TeeItUp needs a clean IP (Oracle VM / unblocker).
Does NOT push to D1.

Usage:
    xvfb-run -a python -m scraper.probe_teeitup_patchright --date 2026-08-24
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

log = logging.getLogger("teetime")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
API_BASE = "https://phx-api-be-east-1b.kenna.io"

# In-page (real browser TLS + a legit *.book.teeitup.com Origin/Referer). kenna's
# CORS is permissive across teeitup booking origins, so one loaded origin can query
# the whole fleet cross-alias. tee-times is scoped by the x-be-alias header alone
# (facility_id is None for these courses), matching the production plain path.
FLOW_JS = r"""
async ([apiBase, aliases, dateIso]) => {
  const out = [];
  for (const alias of aliases) {
    const rec = {alias: alias};
    try {
      const fr = await fetch(apiBase + "/alias/" + alias + "/facilities",
                             {headers: {"x-be-alias": alias}});
      const ft = await fr.text();
      let fn = null, fp = false;
      try { const fj = JSON.parse(ft); fn = (Array.isArray(fj) ? fj : (fj.courses || [])).length; }
      catch (e) { fp = true; }
      rec.fac_status = fr.status; rec.fac_n = fn; rec.fac_parse_failed = fp;
      const tr = await fetch(apiBase + "/v2/tee-times?date=" + encodeURIComponent(dateIso),
                             {headers: {"x-be-alias": alias}});
      const tt = await tr.text();
      let slots = null, tp = false;
      try {
        const tj = JSON.parse(tt);
        const bl = Array.isArray(tj) ? tj : [tj];
        slots = bl.reduce((a, b) => a + ((b && b.teetimes) ? b.teetimes.length : 0), 0);
      } catch (e) { tp = true; }
      rec.tt_status = tr.status; rec.slots = slots; rec.tt_parse_failed = tp;
      rec.tt_bytes = tt.length;
      rec.ok = (tr.status === 200 && !tp && slots > 0);
    } catch (e) { rec.error = String(e).slice(0, 140); }
    out.push(rec);
  }
  return out;
}
"""


def probe(aliases, origins, date_iso, channel, headless):
    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless, "args": ["--no-sandbox"]}
        try:
            browser = pw.chromium.launch(channel=channel, **launch_kwargs)
            launched = f"channel={channel}"
        except Exception as e:  # noqa: BLE001
            log.info("channel=%s unavailable (%s) -> bundled chromium", channel, type(e).__name__)
            browser = pw.chromium.launch(**launch_kwargs)
            launched = "chromium"
        try:
            ctx = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()
            loaded = None
            for u in origins:
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(1500)
                    loaded = u
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not loaded:
                return {"launched": launched, "error": "no booking origin loaded",
                        "origins_tried": origins}
            res = page.evaluate(FLOW_JS, [API_BASE, aliases, date_iso])
            return {"launched": launched, "origin": loaded, "date": date_iso, "results": res}
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Patchright TeeItUp/kenna probe")
    p.add_argument("--aliases", default=("antler-creek-golf-course,buffalo-run-golf-course,"
                   "colorado-national-golf-club,commonground-golf-course,"
                   "four-mile-ranch-golf-club,greenway-park-golf-course,plum-creek-golf-club-3"))
    p.add_argument("--origins", default=("https://antler-creek-golf-course.book.teeitup.com/,"
                   "https://commonground-golf-course.book.teeitup.com/,"
                   "https://colorado-national-golf-club.book.teeitup.com/,"
                   "https://buffalo-run-golf-course.book.teeitup.golf/"))
    p.add_argument("--date", default="", help="ISO date (blank = today+14, a FAR date)")
    p.add_argument("--channel", default=os.environ.get("TU_CHANNEL", "chrome"))
    p.add_argument("--headless", default=os.environ.get("TU_HEADLESS", "0"))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    date_iso = a.date.strip() or (dt.date.today() + dt.timedelta(days=14)).isoformat()
    aliases = [x.strip() for x in a.aliases.split(",") if x.strip()]
    origins = [x.strip() for x in a.origins.split(",") if x.strip()]
    headless = str(a.headless).strip() in ("1", "true", "True", "yes")
    log.info("PROBE teeitup patchright: %d aliases, date=%s, channel=%s, headless=%s",
             len(aliases), date_iso, a.channel, headless)

    r = probe(aliases, origins, date_iso, a.channel, headless)
    print("\n===== TEEITUP PATCHRIGHT PROBE (date " + date_iso + ") =====")
    print(json.dumps(r, indent=2))
    if isinstance(r.get("results"), list):
        served = [x["alias"] for x in r["results"] if x.get("ok")]
        c429 = [x["alias"] for x in r["results"]
                if x.get("tt_status") == 429 or x.get("tt_parse_failed")]
        print(f"\nSERVED {len(served)}/{len(aliases)}: {', '.join(served) or '(none)'}")
        print(f"THROTTLED/429: {', '.join(c429) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
