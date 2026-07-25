"""The two leads still open after #67/#68, probed in a real browser.

A. Arizona Traditions (#68). Its own site links an EZLinks portal,
   arizonatraditionseagle, but plain HTTP gets a Cloudflare 403 on both
   ezlinks hosts (probe-results/verify_az.txt section B) — which proves
   nothing, because the fleet reads EZLinks through browser_ezlinks anyway.
   So run the portal through the SAME flow the fleet uses. If it returns rows,
   the row moves off GolfNow; if the portal is not a real EZLinks tenant, it
   stays on GolfNow and we stop revisiting it.

B. Emerald Greens and University of Denver at Highlands Ranch (#67). Both sit
   in the registry as clubprophet/"ready" with a tenant but no website_id and
   no course_ids — and browser_cps only fetches courses that HAVE both, so
   neither has ever been attempted on any scrape. Their guessed tenants
   404 on the token endpoint (diag4). This checks a short list of plausible
   tenant subdomains for each, in a browser, and reports which (if any) mint
   an anonymous token. A tenant that answers 404 everywhere is not a live CPS
   setup and the registry should say needs_ids rather than ready.

Public pages only. No CAPTCHA or interactive challenge is solved — a portal
still showing one is reported and skipped. No credentials, no TLS forgery.

Report only. Nothing here edits the CSV or the registry.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright  # noqa: E402

from scraper.browser_ezlinks import FLOW_JS as EZ_FLOW  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DATES = [dt.date.today() + dt.timedelta(days=d) for d in (1, 3)]

# Portal spellings worth trying: the CSV/registry one, plus the lowercase form
# (build_registry's ezlinks regex is lowercase-only) and a control that we know
# publishes every day, so a total wipeout is distinguishable from a bad portal.
EZ_PORTALS = [
    ("Arizona Traditions Golf Club", "arizonatraditionseagle"),
    ("Arizona Traditions Golf Club", "arizonatraditions"),
    ("CONTROL — Ocotillo Golf Club", "ocotillo"),
]

# CPS tenant guesses. The registry's current guesses are first.
CPS_CANDIDATES = [
    ("Emerald Greens Golf Club", [
        "emeraldgreens", "windsorgardens", "windsorgardensdenver",
        "emeraldgreensco", "emeraldgreensgolf",
    ]),
    ("University of Denver Golf Club at Highlands Ranch", [
        "universityofdenver", "highlandsranchgolf", "dugolf",
        "duhighlandsranch", "highlandsranch",
    ]),
    # Controls: one live tenant, so a sweep that finds nothing anywhere is
    # visibly a harness problem rather than a finding.
    ("CONTROL — Indian Peaks", ["indianpeaks"]),
]

# Mint the anonymous short-lived token the CPS adapter uses, from inside the
# page so the request carries the tenant's own origin. Nothing is submitted and
# no account is touched; this is the same unauthenticated call the public tee
# sheet makes on load.
CPS_TOKEN_JS = r"""
async ([tenant]) => {
  const base = "https://" + tenant + ".cps.golf";
  try {
    const r = await fetch(base + "/identityapi/myconnect/token/short",
      {method:"POST",
       headers:{"Content-Type":"application/x-www-form-urlencoded"},
       body:"client_id=onlinereswebshortlived"});
    let hasToken = false;
    try { hasToken = !!(await r.json()).access_token; } catch (e) {}
    return {status: r.status, hasToken};
  } catch (e) { return {status: null, error: String(e).slice(0, 120)}; }
}
"""

CHALLENGE = ("just a moment", "verify you are human", "checking your browser",
             "attention required")


def section_a(page) -> None:
    print("\n" + "=" * 72)
    print("A. Arizona Traditions — EZLinks portal through the fleet's own flow")
    print("=" * 72)
    for name, portal in EZ_PORTALS:
        print(f"\n--- {name}  portal={portal}")
        url = f"https://{portal}.ezlinksgolf.com/index.html#!/search"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(7000)      # let a managed challenge auto-clear
        except Exception as exc:  # noqa: BLE001
            print(f"    navigation failed: {type(exc).__name__}: {str(exc)[:110]}")
            continue
        try:
            html = (page.content() or "").lower()
        except Exception:  # noqa: BLE001
            html = ""
        still = [c for c in CHALLENGE if c in html]
        if still and len(html) < 20000:
            print(f"    CHALLENGE still showing ({still[0]!r}) — left alone, "
                  "not solved")
            continue
        for date in DATES:
            try:
                r = page.evaluate(EZ_FLOW, [date.strftime("%m/%d/%Y")])
            except Exception as exc:  # noqa: BLE001
                print(f"    {date}: evaluate failed {type(exc).__name__}: "
                      f"{str(exc)[:100]}")
                continue
            stage, status = r.get("stage"), r.get("status")
            rows = r.get("rows") or []
            err = r.get("error")
            print(f"    {date}: stage={stage} status={status} "
                  f"rows={len(rows)}" + (f" error={err!r}" if err else ""))
            if rows:
                s = rows[0]
                keys = sorted(s.keys())[:8] if isinstance(s, dict) else s
                print(f"        first row keys: {keys}")
        sys.stdout.flush()


def section_b(page) -> None:
    print("\n" + "=" * 72)
    print("B. Club Prophet tenants — does any candidate mint a token?")
    print("=" * 72)
    for name, tenants in CPS_CANDIDATES:
        print(f"\n--- {name}")
        for tenant in tenants:
            try:
                page.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                          wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(4000)
            except Exception as exc:  # noqa: BLE001
                print(f"    {tenant:24s} navigation failed: "
                      f"{type(exc).__name__}: {str(exc)[:80]}")
                continue
            try:
                title = (page.title() or "")[:60]
            except Exception:  # noqa: BLE001
                title = ""
            try:
                r = page.evaluate(CPS_TOKEN_JS, [tenant])
            except Exception as exc:  # noqa: BLE001
                print(f"    {tenant:24s} evaluate failed: {type(exc).__name__}")
                continue
            verdict = ("LIVE (token minted)" if r.get("hasToken")
                       else f"no token (HTTP {r.get('status')})")
            print(f"    {tenant:24s} {verdict}  title={title!r}"
                  + (f"  {r.get('error')}" if r.get("error") else ""))
            sys.stdout.flush()


def main() -> None:
    print("probe_open_leads: the two leads still open after #67/#68")
    print(f"dates: {', '.join(d.isoformat() for d in DATES)}")
    print("Real headless Chromium on public pages. No challenge is solved, no "
          "credentials are entered, no TLS fingerprint is forged.")
    print("Report only. Nothing here edits the CSV or the registry.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(30000)
        for fn in (section_a, section_b):
            try:
                fn(page)
            except Exception as exc:  # noqa: BLE001
                print(f"    HARNESS ERROR in {fn.__name__}: "
                      f"{type(exc).__name__}: {str(exc)[:160]}")
            sys.stdout.flush()
        ctx.close()
        browser.close()
    print("\ndone")


if __name__ == "__main__":
    main()
