"""One-shot RESIDENTIAL proxy diagnostic — why don't the challenged cps
tenants clear through TEEITUP_PROXY (the residential secret)?

Read-only, no D1. Prints FULL exceptions. Three stages, each isolating one
failure world:

  1. plain GET via the secret AS-IS (no sticky mangling) to a neutral IP-echo
     — does the endpoint/auth work AT ALL? Run twice to show whether the exit
     IP rotates per request.
  2. plain GET via the STICKY-mangled username browser_cps builds (provider-
     aware: Webshare `-{sid}` / DataImpulse `;sessid.`) — is the sticky
     SYNTAX valid at the proxy? Run twice: same exit IP both times = sticky
     works; different = sticky syntax silently ignored; 407/exc = syntax
     rejected.
  3. real Chromium via browser_cps._proxy_launch_kwargs(session) against ONE
     challenged tenant (fossiltrace): goto search-teetime, wait for the
     challenge, then fetch Home/Configuration in-page — status 200 with JSON
     means Cloudflare cleared; anything else is printed.

Credentials are NEVER printed — only scheme://host:port and the APPENDED
sticky suffix (derived from the session label, not the secret).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

import requests

from scraper.browser_cps import _proxy_launch_kwargs

IPECHO = "https://api.ipify.org?format=json"
TENANT = "fossiltrace"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _masked(raw: str) -> str:
    try:
        u = urllib.parse.urlparse(raw)
        return f"{u.scheme}://{u.hostname or '?'}" + (f":{u.port}" if u.port else "")
    except Exception:  # noqa: BLE001
        return "(unparseable)"


def _ip(proxies: dict, label: str) -> str | None:
    try:
        r = requests.get(IPECHO, timeout=25, proxies=proxies,
                         headers={"User-Agent": UA})
        ip = ""
        try:
            ip = r.json().get("ip", "")
        except Exception:  # noqa: BLE001
            pass
        print(f"DIAGR {label}: status={r.status_code} ip={ip}", flush=True)
        return ip or None
    except Exception as e:  # noqa: BLE001
        print(f"DIAGR {label}: EXC {type(e).__name__}: {str(e)[:400]}", flush=True)
        return None


def main() -> int:
    raw = re.sub(r"\s+", "", os.environ.get("TEEITUP_PROXY", ""))
    if not raw:
        print("DIAGR no TEEITUP_PROXY set — nothing to test", flush=True)
        return 0
    if "://" not in raw:
        print("DIAGR NOTE: secret has no scheme — normalising to http:// "
              "(same as prod code since the 2026-08-06 fix)", flush=True)
        raw = "http://" + raw
    pu = urllib.parse.urlparse(raw)
    print(f"DIAGR proxy={_masked(raw)} host-has-webshare={'webshare' in (pu.hostname or '').lower()}",
          flush=True)

    # --- stage 1: secret as-is, twice ---
    plain = {"http": raw, "https": raw}
    ip1 = _ip(plain, "as-is #1")
    ip2 = _ip(plain, "as-is #2")
    if ip1 and ip2:
        print(f"DIAGR as-is rotation: {'SAME IP (static/sticky)' if ip1 == ip2 else 'ROTATES per request'}",
              flush=True)

    # --- stage 2: sticky-mangled username, twice ---
    kw = _proxy_launch_kwargs("diagsession-fixed")
    prox = kw.get("proxy")
    if not prox:
        print("DIAGR sticky: _proxy_launch_kwargs returned no proxy — secret unreadable?",
              flush=True)
        return 0
    base_user = urllib.parse.unquote(pu.username or "")
    mangled = prox.get("username", "")
    suffix = mangled[len(base_user):] if mangled.startswith(base_user) else "(REWRITTEN)"
    print(f"DIAGR sticky username: base-len={len(base_user)} appended-suffix={suffix!r}",
          flush=True)
    stick_url = (f"{pu.scheme}://{urllib.parse.quote(mangled, safe='')}:"
                 f"{urllib.parse.quote(urllib.parse.unquote(pu.password or ''), safe='')}"
                 f"@{pu.hostname}" + (f":{pu.port}" if pu.port else ""))
    sticky = {"http": stick_url, "https": stick_url}
    s1 = _ip(sticky, "sticky #1")
    s2 = _ip(sticky, "sticky #2")
    if s1 and s2:
        print(f"DIAGR sticky verdict: {'STICKY WORKS (same IP)' if s1 == s2 else 'NOT STICKY (ip changed) — suffix ignored or wrong syntax'}",
              flush=True)
    elif not s1 and ip1:
        print("DIAGR sticky verdict: STICKY SYNTAX REJECTED by proxy (as-is works, mangled fails)",
              flush=True)

    # --- stage 3: real Chromium via the exact prod path ---
    print("DIAGR --- chromium stage ---", flush=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**_proxy_launch_kwargs("diagsession2"))
            try:
                page = browser.new_page(user_agent=UA)
                page.goto(f"https://{TENANT}.cps.golf/onlineresweb/search-teetime",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(7000)
                r = page.evaluate(
                    """async () => {
                         const resp = await fetch('/onlineresweb/Home/Configuration');
                         const text = await resp.text();
                         let parsed = false;
                         try { JSON.parse(text); parsed = true; } catch (e) {}
                         return {status: resp.status, bytes: text.length,
                                 json: parsed, title: document.title};
                       }""")
                print(f"DIAGR chromium {TENANT}: {json.dumps(r)}", flush=True)
                if r.get("status") == 200 and r.get("json"):
                    print("DIAGR VERDICT: BROWSER+RESIDENTIAL CLEARS — prod failure "
                          "must be elsewhere (timing/concurrency); rerun the scrape",
                          flush=True)
                else:
                    print("DIAGR VERDICT: browser did NOT clear (challenge page or "
                          "non-JSON config) — see status/title above", flush=True)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        print(f"DIAGR chromium: EXC {type(e).__name__}: {str(e)[:500]}", flush=True)
        print("DIAGR VERDICT: Chromium could not even complete the flow — if the "
              "sticky stage above also failed, fix the sticky syntax/auth first",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
