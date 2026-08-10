"""PROBE: can a STEALTH browser (Patchright) clear cps.golf's Cloudflare from a
DATACENTER IP with NO proxy — where vanilla Playwright gets 403?

Why this exists
---------------
Our production browser_cps.py uses vanilla Playwright. Vanilla Playwright leaks
the `Runtime.enable` CDP signal, and Cloudflare's managed challenge watches for
exactly that. So "a real headless browser from GitHub's datacenter" still 403'd on
the 16 hard cps tenants — while the 3 tenants with NO managed challenge cleared
from the SAME datacenter IP. That split proves the 16 are doing bot-DETECTION, not
a blanket IP block. Patchright is a drop-in Playwright fork that patches the
Runtime.enable leak (and other tells), so a genuine browser passes the challenge
without looking automated.

This probe runs the SAME anonymous cps flow (token -> [discover] -> RegisterTxn ->
TeeTimes) inside a Patchright browser, from the free GitHub runner, no proxy, and
reports per-tenant whether Cloudflare cleared and how many tee times came back.
It does NOT push to D1 — it only answers "does the stealth browser get through?"

Usage:
    xvfb-run -a python -m scraper.probe_cps_patchright \
        --tenants cattailcreek,flatironsgolf,marianabutte --date 2026-08-12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

log = logging.getLogger("teetime")

# A recent real desktop Chrome UA (do not advertise HeadlessChrome).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

# The anonymous cps flow, run IN-PAGE (browser TLS + tenant origin + the
# cf_clearance cookie the challenge just set). Copied from browser_cps.FLOW_JS so
# this probe is self-contained (no import-time coupling to the production module).
# Passing wid/cids as "" makes it self-discover via Home/Configuration +
# GetAllOptions, so a stale registry pin can't skew the result.
FLOW_JS = r"""
async ([tenant, wid, cids, dates]) => {
  const base = "https://" + tenant + ".cps.golf";
  const api = base + "/onlineres/onlineapi/api/v1/onlinereservation";
  const ZG = "00000000-0000-0000-0000-000000000000";
  let token = "", apiKey = "";
  try {
    const cfg = await fetch(base + "/onlineresweb/Home/Configuration")
                  .then(r => r.ok ? r.json() : null);
    if (cfg) {
      if (cfg.apiKey) apiKey = cfg.apiKey;
      if (!wid && cfg.websiteId && cfg.websiteId !== ZG) wid = cfg.websiteId;
    }
  } catch (e) {}
  const mintToken = async () => {
    const tr = await fetch(base + "/identityapi/myconnect/token/short",
      {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
       body:"client_id=onlinereswebshortlived"});
    let t = "";
    if (tr.status === 200) { try { t = (await tr.json()).access_token || ""; } catch (e) {} }
    return {status: tr.status, token: t};
  };
  let tk = await mintToken();
  token = tk.token;
  if (!token && !apiKey) return {stage: "token", status: tk.status, results: []};
  const H = (w) => { const h = {"client-id":"onlineresweb",
    "x-terminalid":"3", "x-websiteid": w || ZG, "x-ismobile":"false", "x-productid":"1",
    "x-componentid":"1", "x-siteid":"1", "x-moduleid":"7",
    "x-timezoneid":"America/Denver", "x-timezone-offset":"360",
    "x-requestid":crypto.randomUUID(), "Accept":"application/json"};
    if (token) h["Authorization"] = "Bearer " + token;
    if (apiKey) h["x-apiKey"] = apiKey;
    return h; };
  if (!wid || !cids) {
    const dr = await fetch(api + "/GetAllOptions/" + tenant, {headers:H(ZG)});
    const dtext = await dr.text();
    let db;
    try { db = JSON.parse(dtext); }
    catch (e) { return {stage: "discover", status: dr.status, parse_failed: true,
                        bytes: dtext.length, results: []}; }
    if (!wid) wid = db.webSiteId || (db.reservationOptions && db.reservationOptions.webSiteId) || "";
    if (!cids) {
      const ids = (db.courseOptions || []).map(c => c.courseId != null ? c.courseId : c.id)
                    .filter(x => x != null);
      cids = ids.join(",");
    }
  }
  if (!wid || !cids) return {stage: "discover", status: 200, results: [], note: "no wid/cids"};
  const teeOne = async (date) => {
    const txid = crypto.randomUUID();
    await fetch(api + "/RegisterTransactionId", {method:"POST",
      headers:{...H(wid), "Content-Type":"application/json"},
      body:JSON.stringify({transactionId:txid})});
    const url = api + `/TeeTimes?searchDate=${encodeURIComponent(date)}&holes=0`
      + `&numberOfPlayer=0&courseIds=${cids}&searchTimeType=0&transactionId=${txid}`
      + `&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true&teeSheetSearchView=5`
      + `&classCode=R&defaultOnlineRate=N&isUseCapacityPricing=false&memberStoreId=1&searchType=1`;
    return await fetch(url, {headers:H(wid)});
  };
  const results = [];
  for (const date of dates) {
    try {
      let tt = await teeOne(date);
      if (tt.status === 401 && !apiKey) {
        const rt = await mintToken();
        if (rt.token) { token = rt.token; tt = await teeOne(date); }
      }
      const text = await tt.text();
      let content;
      try { content = (JSON.parse(text)).content || []; }
      catch (e) { results.push({date: date, status: tt.status, parse_failed: true,
                                bytes: text.length}); continue; }
      results.push({date: date, status: tt.status, n: content.length});
    } catch (e) {
      results.push({date: date, status: -1, error: String(e)});
    }
  }
  return {stage: "teetimes", status: 200, wid: wid, cids: cids, results: results};
}
"""


def probe(tenants: list[str], dates: list[dt.date], channel: str, headless: bool) -> dict:
    from patchright.sync_api import sync_playwright

    humans = [d.strftime("%a %b %d %Y") for d in dates]
    out: dict = {}
    with sync_playwright() as pw:
        for tenant in tenants:
            info: dict = {"cf_clearance": False, "launched": None}
            browser = None
            try:
                launch_kwargs = {"headless": headless, "args": ["--no-sandbox"]}
                try:
                    browser = pw.chromium.launch(channel=channel, **launch_kwargs)
                    info["launched"] = f"channel={channel}"
                except Exception as e:  # noqa: BLE001
                    log.info("%s: channel=%s unavailable (%s) -> bundled chromium",
                             tenant, channel, type(e).__name__)
                    browser = pw.chromium.launch(**launch_kwargs)
                    info["launched"] = "chromium"
                ctx = browser.new_context(user_agent=USER_AGENT)
                page = ctx.new_page()
                page.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                          wait_until="domcontentloaded", timeout=60000)
                # Poll up to ~30s for the managed challenge to set cf_clearance.
                for _ in range(15):
                    page.wait_for_timeout(2000)
                    try:
                        cookies = ctx.cookies()
                    except Exception:  # noqa: BLE001
                        cookies = []
                    if any(c.get("name") == "cf_clearance" for c in cookies):
                        info["cf_clearance"] = True
                        break
                page.wait_for_timeout(1500)
                r = page.evaluate(FLOW_JS, [tenant, "", "", humans])
                info["stage"] = r.get("stage")
                info["flow_status"] = r.get("status")
                info["wid"] = r.get("wid")
                info["cids"] = r.get("cids")
                if r.get("note"):
                    info["note"] = r.get("note")
                info["dates"] = {
                    item.get("date"): {"tt_status": item.get("status"),
                                       "n": item.get("n"),
                                       "parse_failed": item.get("parse_failed", False)}
                    for item in (r.get("results") or [])
                }
                # Headline: did we get real tee times for any date?
                info["cleared"] = any(
                    (d.get("tt_status") == 200 and not d.get("parse_failed"))
                    for d in info["dates"].values())
            except Exception as e:  # noqa: BLE001
                info["error"] = f"{type(e).__name__}: {e}"[:200]
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
            out[tenant] = info
            log.info("%-16s -> %s", tenant, json.dumps(info))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Patchright stealth-browser cps probe")
    p.add_argument("--tenants", default="cattailcreek,flatironsgolf,marianabutte",
                   help="comma-separated cps tenant subdomains")
    p.add_argument("--date", default="", help="single ISO date (blank = today+2)")
    p.add_argument("--dates", default="", help="comma-separated ISO dates (overrides --date)")
    p.add_argument("--channel", default=os.environ.get("CPS_PATCH_CHANNEL", "chrome"),
                   help="browser channel to try first (chrome|chromium|msedge)")
    p.add_argument("--headless", default=os.environ.get("CPS_HEADLESS", "0"),
                   help="1=headless, 0=headful (run under xvfb)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    if a.dates.strip():
        dates = [dt.date.fromisoformat(s.strip()) for s in a.dates.split(",") if s.strip()]
    elif a.date.strip():
        dates = [dt.date.fromisoformat(a.date.strip())]
    else:
        dates = [dt.date.today() + dt.timedelta(days=2)]

    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]
    headless = str(a.headless).strip() in ("1", "true", "True", "yes")
    log.info("PROBE patchright: %d tenants, dates=%s, channel=%s, headless=%s",
             len(tenants), [d.isoformat() for d in dates], a.channel, headless)

    result = probe(tenants, dates, a.channel, headless)

    cleared = [t for t, v in result.items() if v.get("cleared")]
    print("\n===== PATCHRIGHT CPS PROBE SUMMARY =====")
    print(json.dumps(result, indent=2))
    print(f"\nCLEARED {len(cleared)}/{len(tenants)}: {', '.join(cleared) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
