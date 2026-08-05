"""Datacenter-reachability probe for Club Prophet (cps.golf) — read only.

QUESTION. cps currently scrapes through a headless browser + residential proxy
because cps.golf sits behind Cloudflare and its clearance is IP-bound (a ROTATING
proxy broke it: 19/27 tenants 403'd at TeeTimes — see browser_cps.py). The
residential proxy is now dead. Can any cps tenants be scraped over PLAIN HTTP
straight from a datacenter IP (no proxy, no browser)? That is only possible for
tenants whose Cloudflare is NOT in an aggressive/challenge mode.

WHAT THIS DOES. For every unique cps tenant, it runs the FULL reservation flow
(the browser_cps FLOW_JS ported to `requests`) datacenter-direct: one keep-alive
Session per tenant, so all 5 requests egress the SAME runner IP and any IP-bound
Cloudflare cookie holds. It fetches real tee times for a near date and reports,
per tenant, how far it got and whether Cloudflare challenged it. NOTHING is
written to D1 — pure measurement.

Reading the result:
  teetimes>0                → this tenant works datacenter-direct (move it off
                              residential; may still need rotation for rate
                              limits, but Cloudflare is not blocking it).
  challenged=YES (403/503)  → Cloudflare blocks the datacenter IP; this tenant
                              still needs the residential browser.
  stage=token, none         → neither token nor apiKey (dead/unsupported tenant).

Usage:  python -m scraper.probe_cps_dc [--date YYYY-MM-DD]
Set TEEITUP_PROXY to also route through a proxy (NOT recommended for cps — a
rotating proxy breaks the IP-bound cookie; only a sticky proxy would hold).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid

import requests

from .aggregate import load_registry

API_PATH = "/onlineres/onlineapi/api/v1/onlinereservation"
ZG = "00000000-0000-0000-0000-000000000000"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 25

# Subdomains that are broken in the registry; map/skip so the probe is clean.
_SUB_FIX = {"e": "cranelakes"}     # crane-lakes registry typo (known)
_SUB_SKIP = {None, ""}


def _cps_subs(registry_path: str) -> list[str]:
    reg = load_registry(registry_path)
    courses = reg if isinstance(reg, list) else reg.get("courses", [])
    subs: list[str] = []
    seen = set()
    for c in courses:
        if not isinstance(c, dict) or c.get("platform") != "clubprophet":
            continue
        m = re.search(r"https?://([a-z0-9-]+)\.cps\.golf", c.get("booking_url", "") or "")
        sub = m.group(1) if m else None
        sub = _SUB_FIX.get(sub, sub)
        if sub in _SUB_SKIP or sub in seen:
            continue
        seen.add(sub)
        subs.append(sub)
    return subs


def _challenged(resp) -> bool:
    """True if this response looks like a Cloudflare bot challenge/block."""
    if resp.status_code in (403, 503):
        return True
    if "cf-mitigated" in {k.lower() for k in resp.headers}:
        return True
    body = (resp.text or "")[:600].lower()
    return ("just a moment" in body or "cf-challenge" in body
            or "challenge-platform" in body or "attention required" in body)


def _headers(w: str, token: str, api_key: str) -> dict:
    h = {"client-id": "onlineresweb", "x-terminalid": "3",
         "x-websiteid": w or ZG, "x-ismobile": "false", "x-productid": "1",
         "x-componentid": "1", "x-siteid": "1", "x-moduleid": "7",
         "x-timezoneid": "America/Denver", "x-timezone-offset": "360",
         "x-requestid": str(uuid.uuid4()), "Accept": "application/json",
         "User-Agent": UA}
    if token:
        h["Authorization"] = "Bearer " + token
    if api_key:
        h["x-apiKey"] = api_key
    return h


def probe_tenant(sub: str, date_str: str, proxies: dict | None) -> dict:
    """Run the full cps flow datacenter-direct. Returns a result dict."""
    base = f"https://{sub}.cps.golf"
    api = base + API_PATH
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxies:
        s.proxies.update(proxies)
    r = {"sub": sub, "stage": "config", "status": 0, "teetimes": 0,
         "challenged": False, "auth": "none"}

    def get(url, **kw):
        return s.get(url, timeout=TIMEOUT, **kw)

    # 1) Configuration → apiKey / websiteId
    token, api_key, wid = "", "", ""
    try:
        cfg = get(base + "/onlineresweb/Home/Configuration")
        r["status"] = cfg.status_code
        if _challenged(cfg):
            r["challenged"] = True; r["stage"] = "config"; return r
        if cfg.ok:
            try:
                j = cfg.json()
                if isinstance(j, dict):
                    api_key = j.get("apiKey") or ""
                    if j.get("websiteId") and j["websiteId"] != ZG:
                        wid = j["websiteId"]
            except ValueError:
                pass
    except Exception as e:  # noqa: BLE001
        r["stage"] = "config"; r["error"] = type(e).__name__; return r

    # 2) token/short (anonymous Bearer variant)
    try:
        tr = s.post(base + "/identityapi/myconnect/token/short",
                    data="client_id=onlinereswebshortlived",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=TIMEOUT)
        if _challenged(tr):
            r["challenged"] = True; r["stage"] = "token"; r["status"] = tr.status_code; return r
        if tr.status_code == 200:
            try:
                token = tr.json().get("access_token") or ""
            except ValueError:
                pass
    except Exception:  # noqa: BLE001 — token is optional (apiKey variant)
        pass

    r["auth"] = "token" if token else ("apikey" if api_key else "none")
    if not token and not api_key:
        r["stage"] = "token"; return r

    # 3) discover wid + cids via GetAllOptions
    cids = ""
    try:
        dr = get(api + "/GetAllOptions/" + sub, headers=_headers(ZG, token, api_key))
        r["status"] = dr.status_code
        if _challenged(dr):
            r["challenged"] = True; r["stage"] = "discover"; return r
        try:
            db = dr.json()
        except ValueError:
            r["stage"] = "discover"; r["parse_failed"] = True; return r
        if not wid:
            wid = db.get("webSiteId") or (db.get("reservationOptions") or {}).get("webSiteId") or ""
        ids = [c.get("courseId") if c.get("courseId") is not None else c.get("id")
               for c in (db.get("courseOptions") or [])]
        cids = ",".join(str(x) for x in ids if x is not None)
    except Exception as e:  # noqa: BLE001
        r["stage"] = "discover"; r["error"] = type(e).__name__; return r

    if not wid or not cids:
        r["stage"] = "discover"; r["note"] = "no wid/cids"; return r

    # 4) RegisterTransactionId
    txid = str(uuid.uuid4())
    try:
        hr = _headers(wid, token, api_key); hr["Content-Type"] = "application/json"
        rt = s.post(api + "/RegisterTransactionId", headers=hr,
                    json={"transactionId": txid}, timeout=TIMEOUT)
        if _challenged(rt):
            r["challenged"] = True; r["stage"] = "register"; r["status"] = rt.status_code; return r
    except Exception as e:  # noqa: BLE001
        r["stage"] = "register"; r["error"] = type(e).__name__; return r

    # 5) TeeTimes
    url = (f"{api}/TeeTimes?searchDate={date_str}&holes=0&numberOfPlayer=0"
           f"&courseIds={cids}&searchTimeType=0&transactionId={txid}"
           f"&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true"
           f"&teeSheetSearchView=5&classCode=R&defaultOnlineRate=N"
           f"&isUseCapacityPricing=false&memberStoreId=1&searchType=1")
    try:
        tt = get(url, headers=_headers(wid, token, api_key))
        r["status"] = tt.status_code; r["stage"] = "teetimes"
        if _challenged(tt):
            r["challenged"] = True; return r
        try:
            content = (tt.json() or {}).get("content") or []
            r["teetimes"] = len([x for x in content if isinstance(x, dict)])
        except ValueError:
            r["parse_failed"] = True
    except Exception as e:  # noqa: BLE001
        r["error"] = type(e).__name__
    return r


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="cps datacenter-direct reachability probe")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between tenants")
    a = p.parse_args(argv)

    raw = os.environ.get("TEEITUP_PROXY", "").strip()
    raw = re.sub(r"\s+", "", raw)
    proxies = {"http": raw, "https": raw} if raw else None
    print(f"CPSDC start date={a.date} proxy={'on' if proxies else 'DIRECT'}", flush=True)

    subs = _cps_subs(a.registry)
    print(f"CPSDC tenants={len(subs)}", flush=True)
    ok = challenged = dead = 0
    for sub in subs:
        r = probe_tenant(sub, a.date, proxies)
        tag = ("OK" if r["teetimes"] > 0 else
               "CHALLENGED" if r["challenged"] else
               "no-auth" if r["stage"] == "token" and r["auth"] == "none" else
               "empty/fail")
        if r["teetimes"] > 0: ok += 1
        elif r["challenged"]: challenged += 1
        elif r["stage"] == "token" and r["auth"] == "none": dead += 1
        print("CPSDC %-20s %-11s stage=%-8s status=%s teetimes=%d auth=%s%s"
              % (sub, tag, r["stage"], r["status"], r["teetimes"], r["auth"],
                 (" err=" + r["error"]) if r.get("error") else
                 (" parse_failed" if r.get("parse_failed") else "")), flush=True)
        time.sleep(a.gap)
    print("CPSDC DONE ok=%d challenged=%d no-auth=%d other=%d of %d"
          % (ok, challenged, dead, len(subs) - ok - challenged - dead, len(subs)),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
