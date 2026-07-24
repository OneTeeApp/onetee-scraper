"""Probe: can a LONGER managed-challenge settle + fresh context per tenant get
more cps.golf tenants past the WAF from GitHub's IP? Currently only 2 of 16
land in D1. EZLinks needed ~7s for Cloudflare's managed JS to auto-clear; the
cps fetcher only waits 800ms. Test 6s settle + isolated context + 4 retries
across ALL tenants and report the pass rate — no stealth, no challenge-solving.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")
from scraper.aggregate import load_registry            # noqa: E402
from scraper.browser_cps import FLOW_JS                # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DATE = (dt.date.today() + dt.timedelta(days=1)).strftime("%a %b %d %Y")


def try_tenant(pw, tenant, wid, cids, settle_ms, retries):
    """Fresh context per attempt; long settle for the managed challenge."""
    last = None
    for attempt in range(retries):
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA)
            pg.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                    wait_until="domcontentloaded", timeout=35000)
            pg.wait_for_timeout(settle_ms)
            r = pg.evaluate(FLOW_JS, [tenant, wid, cids, DATE])
            last = f"{r.get('stage')} {r.get('status')}"
            if r.get("status") == 200:
                return True, len(r.get("content") or []), attempt + 1
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        finally:
            b.close()
    return False, last, retries


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    cps = [c for c in reg if c["platform"] == "clubprophet"
           and c["ids"].get("tenant") and c["ids"].get("course_ids")]
    ok = 0
    with sync_playwright() as pw:
        for c in cps:
            ids = c["ids"]
            wid = ids.get("website_id") or ""
            cids = ",".join(str(x) for x in ids["course_ids"])
            passed, info, tries = try_tenant(pw, ids["tenant"], wid, cids,
                                             settle_ms=6000, retries=4)
            if passed:
                ok += 1
                print(f"RESULT cps {c['slug']:<40} PASS {info} times (try {tries})",
                      flush=True)
            else:
                print(f"RESULT cps {c['slug']:<40} FAIL {info}", flush=True)
    print(f"RESULT SUMMARY: {ok}/{len(cps)} tenants passed with 6s settle + "
          f"fresh context", flush=True)


if __name__ == "__main__":
    main()
