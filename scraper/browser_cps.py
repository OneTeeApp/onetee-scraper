"""Headless-browser fetcher for Club Prophet (cps.golf) tenants.

Some cps.golf tenants (City of Boulder / Flatirons, Fossil Trace, ...) run a WAF
that 403s the plain HTTP client's TLS fingerprint from a datacenter IP, while
letting a real browser through. Proven on GitHub's runner: Flatirons/Fossil
returned 200 via a headless Chromium and 403 via `requests`; Indian Peaks works
either way. So we run the SAME public anonymous token->register->TeeTimes flow
(see adapters/clubprophet.py) inside a real Chromium via Playwright and emit an
aggregate-format JSON document for `scraper.d1 push`.

This owns ALL clubprophet courses (the plain scraper excludes the platform), so
the two never write the same course_slug and clobber each other in D1.

Usage:
    python -m scraper.browser_cps --date 2026-07-24 --out output/cps.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import urllib.parse

from .adapters.base import USER_AGENT
from .adapters.clubprophet import ClubProphetAdapter
from .aggregate import load_registry
from .sharding import apply_shard, set_env_shard_count

log = logging.getLogger("teetime")

# The anonymous flow, run inside the page (real browser TLS + tenant origin).
FLOW_JS = r"""
async ([tenant, wid, cids, date]) => {
  const base = "https://" + tenant + ".cps.golf";
  const api = base + "/onlineres/onlineapi/api/v1/onlinereservation";
  const ZG = "00000000-0000-0000-0000-000000000000";
  let token;
  const H = (w) => ({Authorization:"Bearer "+token, "client-id":"onlineresweb",
    "x-terminalid":"3", "x-websiteid": w || ZG, "x-ismobile":"false", "x-productid":"1",
    "x-componentid":"1", "x-siteid":"1", "x-moduleid":"7",
    "x-timezoneid":"America/Denver", "x-timezone-offset":"360",
    "x-requestid":crypto.randomUUID(), "Accept":"application/json"});
  const tr = await fetch(base + "/identityapi/myconnect/token/short",
    {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
     body:"client_id=onlinereswebshortlived"});
  if (tr.status !== 200) return {status: tr.status, stage: "token", content: []};
  token = (await tr.json()).access_token;
  // Discover websiteId + courseIds when the registry did not pin them. Done
  // in-browser (post-challenge) via GetAllOptions, which needs only the token.
  // This is why unpinned tenants (emerald-greens etc.) used to return nothing:
  // run() skipped every course without pinned course_ids. Now nothing is skipped.
  if (!wid || !cids) {
    const dr = await fetch(api + "/GetAllOptions/" + tenant, {headers:H(ZG)});
    const dtext = await dr.text();
    let db;
    try { db = JSON.parse(dtext); }
    catch (e) { return {status: dr.status, stage: "discover", parse_failed: true,
                        bytes: dtext.length}; }
    if (!wid) wid = db.webSiteId || (db.reservationOptions && db.reservationOptions.webSiteId) || "";
    if (!cids) {
      const ids = (db.courseOptions || []).map(c => c.courseId != null ? c.courseId : c.id)
                    .filter(x => x != null);
      cids = ids.join(",");
    }
  }
  if (!wid || !cids) return {status: 200, stage: "discover", content: [], note: "no wid/cids"};
  const txid = crypto.randomUUID();
  await fetch(api + "/RegisterTransactionId", {method:"POST",
    headers:{...H(wid), "Content-Type":"application/json"},
    body:JSON.stringify({transactionId:txid})});
  const url = api + `/TeeTimes?searchDate=${encodeURIComponent(date)}&holes=0`
    + `&numberOfPlayer=0&courseIds=${cids}&searchTimeType=0&transactionId=${txid}`
    + `&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true&teeSheetSearchView=5`
    + `&classCode=R&defaultOnlineRate=N&isUseCapacityPricing=false&memberStoreId=1&searchType=1`;
  const tt = await fetch(url, {headers:H(wid)});
  // Report a non-JSON body AS a failure (same fix as browser_golfnow): a WAF
  // interstitial served with status 200 used to become "0 tee times, success",
  // skipping the remaining attempts and deactivating the tenant's real rows.
  const text = await tt.text();
  let content;
  try { content = (JSON.parse(text)).content || []; }
  catch (e) { return {status: tt.status, stage: "teetimes", parse_failed: true,
                      bytes: text.length}; }
  return {status: tt.status, stage: "teetimes", content};
}
"""


def _teetimes(course: dict, slots: list[dict]) -> list:
    # Label per sub-course when one tenant query spans several (Fossil Trace's
    # 3 nines, GVR's sheets) so same-time slots don't collapse in D1.
    names = {s.get("courseName") for s in slots if s.get("courseName")}
    multi = len(names) > 1
    out = []
    for s in slots:
        t = s.get("startTime")
        if not t:
            continue
        prices = ClubProphetAdapter._prices(s)
        # open_spots via the shared helper: availableParticipantNo is an ARRAY of
        # bookable party sizes, so max() = remaining seats (see adapters/
        # clubprophet.py). The old inline scalar check dropped every row to None.
        out.append(ClubProphetAdapter.base_tee_time(
            course, teetime=str(t), holes=ClubProphetAdapter._holes(s),
            course_label=(s.get("courseName") or "") if multi else "",
            open_spots=ClubProphetAdapter._open_spots(s),
            price_min=min(prices) if prices else None,
            price_max=max(prices) if prices else None,
            raw={"course_name": s.get("courseName", course["name"])}))
    return out


def _proxy_launch_kwargs() -> dict:
    """chromium.launch kwargs, adding a residential proxy if TEEITUP_PROXY is set.

    cps.golf (like kenna) blocks/challenges the data-center IP; a residential
    proxy clears it. Playwright's Chromium ignores HTTPS_PROXY on its own, so the
    proxy MUST be passed to launch(proxy=). TEEITUP_PROXY is the shared secret.
    """
    kwargs = {"args": ["--no-sandbox"]}
    url = os.environ.get("TEEITUP_PROXY", "").strip()
    if url:
        pu = urllib.parse.urlparse(url)
        server = f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")
        proxy = {"server": server}
        if pu.username:
            proxy["username"] = urllib.parse.unquote(pu.username)
        if pu.password:
            proxy["password"] = urllib.parse.unquote(pu.password)
        kwargs["proxy"] = proxy
        log.info("cps: routing Chromium through proxy %s", server)
    return kwargs


def run(date: dt.date, registry_path: str, out_path: str,
        shard: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    set_env_shard_count(shard)
    # course_ids is NO LONGER required — FLOW_JS discovers wid+cids in-browser
    # via GetAllOptions when they aren't pinned, so the ~14 unpinned cps tenants
    # (emerald-greens etc.) that used to be silently skipped are now scraped.
    courses = [c for c in registry
               if c["platform"] == "clubprophet" and c["ids"].get("tenant")]
    courses = apply_shard(courses, shard)
    date_str = date.strftime("%a %b %d %Y")
    log.info("browser-fetching %d cps tenants for %s", len(courses), date)

    # Residential proxy so cps.golf's Cloudflare managed challenge clears from a
    # data-center IP (measured 2026-08-04: indian-tree 0/17, indian-peaks 1/3,
    # emerald-greens 0 from the runner while all full from a residential browser).
    # Same TEEITUP_PROXY secret as browser_teeitup; unset = direct (unchanged).
    _proxy = _proxy_launch_kwargs()

    tee_times, errors = [], []
    with sync_playwright() as pw:
        for c in courses:
            ids = c["ids"]
            tenant = ids["tenant"]
            wid = ids.get("website_id") or ""
            cids = ",".join(str(x) for x in (ids.get("course_ids") or []))
            last = None
            got = False
            for attempt in range(3):
                # Fresh browser per attempt. Cloudflare's managed JS challenge
                # is issued per browsing context and takes ~6s to auto-clear; a
                # clean context that waits it out clears the tenant WAF far more
                # reliably than a reused page with a short settle (2/16 -> 13/13
                # tenants in testing). This is the same legit managed-challenge
                # auto-clear used for EZLinks — no stealth, no challenge-solving.
                browser = pw.chromium.launch(**_proxy)
                try:
                    page = browser.new_page(user_agent=USER_AGENT)
                    page.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                              wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(6000)  # let the managed challenge clear
                    r = page.evaluate(FLOW_JS, [tenant, wid, cids, date_str])
                    last = f"{r.get('stage')} {r.get('status')}" + (
                        " parse_failed" if r.get("parse_failed") else "")
                    if r.get("status") == 200 and not r.get("parse_failed"):
                        tts = _teetimes(c, r.get("content") or [])
                        tee_times.extend(tts)
                        log.info("  %-34s %d times", c["slug"], len(tts))
                        got = True
                except Exception as e:  # noqa: BLE001
                    last = f"{type(e).__name__}"
                finally:
                    browser.close()
                if got:
                    break
                time.sleep(2 * (attempt + 1))    # brief backoff before a fresh try
            if not got:
                errors.append({"course": c["slug"], "platform": "clubprophet",
                               "error": f"browser {last}"})
                log.info("  %-34s ERROR %s", c["slug"], last)

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
    p = argparse.ArgumentParser(description="Browser-based cps.golf fetcher")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--shard", help="i/N — process a 1/N slice")
    p.add_argument("--out", default="output/cps.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.registry, a.out, a.shard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
