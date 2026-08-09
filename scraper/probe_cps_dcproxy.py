"""cps.golf via a STICKY datacenter proxy — read only.

Brian's goal: scrape the ~16 challenged cps tenants without a RESIDENTIAL proxy.
Established so far: from GitHub's datacenter IP they 403 even with a real browser
(claude/cps-browser-direct-result) — it's IP reputation, not fingerprint/JS. The
one lever left: a NON-GitHub datacenter IP with cleaner Cloudflare reputation,
i.e. the cheap TEEITUP_DC_PROXY (Webshare datacenter, flat-rate, no bandwidth
meter) — but STICKY, because cps binds its cf_clearance to the IP (rotating
breaks it).

This runs the full plain reservation flow per tenant through the DC proxy, one
STICKY session per tenant (same exit IP for all requests in the flow), and
reports: the exit IP (to prove sticky held), and per tenant how far it got /
whether Cloudflare still challenged. Plain HTTP first — if a good IP alone clears
these (they're IP-blocked, not JS-challenged like fossil-trace), no browser is
even needed. Writes NOTHING to D1.

Sticky username is provider-aware, mirroring browser_cps._proxy_launch_kwargs:
Webshare -> {base}-{cc}-{sid} (cc from CPS_PROXY_COUNTRY, blank for datacenter);
else append ;sessid.<id>. Proxy secret: TEEITUP_DC_PROXY (falls back to
TEEITUP_PROXY).

Usage: python -m scraper.probe_cps_dcproxy [--date YYYY-MM-DD] [--only sub,sub]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import time
import urllib.parse

import requests

from .probe_cps_dc import _cps_subs, _challenged, _headers, API_PATH, ZG, UA, TIMEOUT


def _sticky_proxies(sid: str):
    raw = re.sub(r"\s+", "", os.environ.get("TEEITUP_DC_PROXY", "")
                 or os.environ.get("TEEITUP_PROXY", ""))
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    pu = urllib.parse.urlparse(raw)
    user = urllib.parse.unquote(pu.username or "")
    pw = urllib.parse.unquote(pu.password or "")
    host = (pu.hostname or "").lower()
    if user and "webshare" in host:
        base = user.split("-", 1)[0]
        cc = os.environ.get("CPS_PROXY_COUNTRY", "").lower()  # blank = no country tag (datacenter)
        num = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) % 1000000
        user = f"{base}-{cc}-{num}" if cc else f"{base}-{num}"
    elif user and "sessid." not in user:
        user = f"{user};sessid.{sid}"
    auth = f"{urllib.parse.quote(user)}:{urllib.parse.quote(pw)}@" if user else ""
    server = f"{pu.scheme}://{auth}{pu.hostname}" + (f":{pu.port}" if pu.port else "")
    return {"http": server, "https": server}


def _exit_ip(proxies) -> str:
    try:
        s = requests.Session(); s.proxies.update(proxies or {})
        r = s.get("https://api.ipify.org?format=json", timeout=TIMEOUT)
        return r.json().get("ip", "?") if r.ok else f"HTTP{r.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {str(e)[:60]}"


def probe_tenant(sub: str, date_str: str) -> dict:
    base = f"https://{sub}.cps.golf"
    api = base + API_PATH
    proxies = _sticky_proxies(sub)
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxies:
        s.proxies.update(proxies)
    r = {"sub": sub, "stage": "config", "status": 0, "teetimes": 0,
         "challenged": False, "auth": "none", "ip": ""}

    # exit IP twice — confirm the sticky session holds ONE IP
    ip1 = _exit_ip(proxies)
    ip2 = _exit_ip(proxies)
    r["ip"] = ip1 + ("" if ip1 == ip2 else f"!={ip2}(ROTATED)")

    def get(url, **kw):
        return s.get(url, timeout=TIMEOUT, **kw)

    token, api_key, wid = "", "", ""
    try:
        cfg = get(base + "/onlineresweb/Home/Configuration")
        r["status"] = cfg.status_code
        if _challenged(cfg):
            r["challenged"] = True; return r
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
        r["error"] = type(e).__name__; return r

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

    import uuid
    txid = str(uuid.uuid4())
    try:
        hr = _headers(wid, token, api_key); hr["Content-Type"] = "application/json"
        rt = s.post(api + "/RegisterTransactionId", headers=hr,
                    json={"transactionId": txid}, timeout=TIMEOUT)
        if _challenged(rt):
            r["challenged"] = True; r["stage"] = "register"; r["status"] = rt.status_code; return r
    except Exception as e:  # noqa: BLE001
        r["stage"] = "register"; r["error"] = type(e).__name__; return r

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


# The ~16 that 403 even with a real browser from GitHub's datacenter IP.
CHALLENGED = [
    "cattailcreek", "flatironsgolf", "fossiltrace", "gypsumcreekgolf",
    "indiantree", "marianabutte", "oldecourseloveland", "universityofdenver",
    "sewailo", "westfields", "oldtrailgc", "williamsburgnatgc", "stonebrookfl",
    "eagleslanding", "manowar", "waradmiral", "rumpointe", "lighthousesound",
    "nutterscrossing", "glenmoor",
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="cps via sticky datacenter proxy")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--only", default="", help="comma-separated subs (blank = the challenged set)")
    p.add_argument("--gap", type=float, default=0.4)
    a = p.parse_args(argv)

    have = set(_cps_subs(a.registry))
    if a.only:
        subs = [x.strip() for x in a.only.split(",") if x.strip()]
    else:
        subs = [s for s in CHALLENGED if s in have]

    proxy_set = bool(re.sub(r"\s+", "", os.environ.get("TEEITUP_DC_PROXY", "")
                            or os.environ.get("TEEITUP_PROXY", "")))
    print(f"CPSDCP date={a.date} DC-proxy={'set' if proxy_set else 'MISSING'} tenants={len(subs)}",
          flush=True)
    if not proxy_set:
        print("CPSDCP no proxy secret — nothing to test", flush=True); return 0

    ok = challenged = other = 0
    for sub in subs:
        r = probe_tenant(sub, a.date)
        tag = ("OK" if r["teetimes"] > 0 else
               "CHALLENGED" if r["challenged"] else "empty/fail")
        if r["teetimes"] > 0: ok += 1
        elif r["challenged"]: challenged += 1
        else: other += 1
        print("CPSDCP %-20s %-11s stage=%-8s status=%s tt=%d ip=%s%s"
              % (sub, tag, r["stage"], r["status"], r["teetimes"], r["ip"],
                 (" err=" + r["error"]) if r.get("error") else ""), flush=True)
        time.sleep(a.gap)
    print(f"CPSDCP DONE ok={ok} challenged={challenged} other={other} of {len(subs)} "
          f"(OK via sticky DC proxy = can drop residential for those)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
