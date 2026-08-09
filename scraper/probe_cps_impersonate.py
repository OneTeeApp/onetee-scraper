"""cps.golf datacenter-direct probe, PLAIN requests vs curl_cffi TLS impersonation.

QUESTION. cps currently pushes ~18 "challenged" tenants through a metered
residential browser because, from a datacenter IP, plain python-requests gets a
Cloudflare 403/503 at the reservation flow. Every prior cps datacenter probe used
stock `requests`. golfrev proved that Cloudflare there was fingerprinting the
python-requests TLS handshake (JA3): plain -> 403, curl_cffi impersonate=chrome
-> 200, with NO proxy. That lever was never tried on cps.

WHAT THIS DOES. For each unique cps tenant it runs the FULL reservation flow
(the same steps as probe_cps_dc) TWICE, datacenter-direct, no proxy:
  - PLAIN:  requests.Session (stock UA)              — the current baseline
  - CHROME: curl_cffi Session(impersonate="chrome")  — the golfrev fix
One keep-alive Session per run so all requests egress the same runner IP and any
IP-bound Cloudflare cookie holds. Reports, per tenant and per mode, how far it
got, the status, teetimes count, and whether Cloudflare challenged it. Writes
NOTHING to D1 — pure measurement.

Reading it: any tenant that is `challenged` under PLAIN but returns `teetimes>0`
under CHROME is one we can move OFF residential onto a plain curl_cffi flow.

Usage:  python -m scraper.probe_cps_impersonate [--date YYYY-MM-DD] [--only sub,sub]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import time
import uuid

import requests

from .probe_cps_dc import (
    API_PATH, ZG, UA, TIMEOUT, _cps_subs, _challenged, _headers,
)

try:
    from curl_cffi import requests as creq
except Exception as e:  # noqa: BLE001
    creq = None
    print("curl_cffi import failed:", e, flush=True)


def _new_session(mode: str):
    """mode: 'plain' -> requests.Session; 'chrome' -> curl_cffi impersonated."""
    if mode == "chrome":
        s = creq.Session(impersonate="chrome")
    else:
        s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def probe_tenant(sub: str, date_str: str, mode: str) -> dict:
    """Run the full cps flow datacenter-direct with the given session mode."""
    base = f"https://{sub}.cps.golf"
    api = base + API_PATH
    s = _new_session(mode)
    r = {"sub": sub, "mode": mode, "stage": "config", "status": 0,
         "teetimes": 0, "challenged": False, "auth": "none"}

    def get(url, **kw):
        return s.get(url, timeout=TIMEOUT, **kw)

    token, api_key, wid = "", "", ""
    # 1) Configuration -> apiKey / websiteId
    try:
        cfg = get(base + "/onlineresweb/Home/Configuration")
        r["status"] = cfg.status_code
        if _challenged(cfg):
            r["challenged"] = True; return r
        if getattr(cfg, "ok", cfg.status_code == 200):
            try:
                j = cfg.json()
                if isinstance(j, dict):
                    api_key = j.get("apiKey") or ""
                    if j.get("websiteId") and j["websiteId"] != ZG:
                        wid = j["websiteId"]
            except ValueError:
                pass
    except Exception as e:  # noqa: BLE001
        r["error"] = type(e).__name__; return r

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
    except Exception:  # noqa: BLE001
        pass

    r["auth"] = "token" if token else ("apikey" if api_key else "none")
    if not token and not api_key:
        r["stage"] = "token"; return r

    # 3) GetAllOptions -> wid + cids
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
    p = argparse.ArgumentParser(description="cps plain-vs-impersonate datacenter probe")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--only", default="", help="comma-separated subs to limit to")
    p.add_argument("--gap", type=float, default=0.4)
    a = p.parse_args(argv)

    subs = _cps_subs(a.registry)
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        subs = [s for s in subs if s in want]
    print(f"CPSIMP date={a.date} curl_cffi={'yes' if creq else 'NO'} tenants={len(subs)}",
          flush=True)
    print(f"{'tenant':22s} {'PLAIN':26s} {'CHROME(impersonate)':26s} verdict", flush=True)

    gained = []
    for sub in subs:
        rp = probe_tenant(sub, a.date, "plain")
        time.sleep(a.gap)
        rc = probe_tenant(sub, a.date, "chrome") if creq else {"teetimes": 0, "challenged": False, "stage": "n/a", "status": 0}
        time.sleep(a.gap)

        def cell(r):
            tag = ("OK" if r["teetimes"] > 0 else
                   "CHALLENGED" if r.get("challenged") else
                   "no-auth" if r.get("stage") == "token" and r.get("auth") == "none" else
                   "empty/fail")
            return f"{tag}(s{r.get('status',0)},tt{r['teetimes']},{r.get('stage','')})"

        verdict = ""
        if rc["teetimes"] > 0 and (rp.get("challenged") or rp["teetimes"] == 0):
            verdict = "*** CHROME GAINS THIS ***"
            gained.append(sub)
        elif rp["teetimes"] > 0 and rc["teetimes"] > 0:
            verdict = "both-ok"
        print(f"{sub:22s} {cell(rp):26s} {cell(rc):26s} {verdict}", flush=True)

    print(f"\nCPSIMP DONE — CHROME gained {len(gained)} tenant(s) plain could not get: "
          + (", ".join(gained) if gained else "(none)"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
