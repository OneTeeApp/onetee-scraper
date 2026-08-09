"""cps.golf via a STICKY datacenter proxy — read only, SELF-TUNING.

Brian's goal: scrape the ~16 challenged cps tenants without a RESIDENTIAL proxy.
Established: from GitHub's datacenter IP they 403 even with a real browser
(claude/cps-browser-direct-result) — IP reputation, not fingerprint/JS. The one
lever left: a NON-GitHub datacenter IP with cleaner Cloudflare reputation, i.e.
the cheap TEEITUP_DC_PROXY (flat-rate, no bandwidth meter) — but STICKY, because
cps binds cf_clearance to the IP.

The proxy's sticky-username format is provider/plan-specific and not knowable
from here, so this probe is SELF-TUNING: at startup it tries several username
variants against an IP-echo and picks the first that authenticates, reporting
whether that IP is stable across two calls on one session (sticky) or rotates.
Then it runs the full plain cps flow per tenant through that variant and reports
whether each clears Cloudflare from the proxy's IP. Writes NOTHING to D1.

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
import uuid

import requests

from .probe_cps_dc import _cps_subs, _challenged, _headers, API_PATH, ZG, UA, TIMEOUT

IPECHO = "https://api.ipify.org?format=json"


def _parse_proxy():
    raw = re.sub(r"\s+", "", os.environ.get("TEEITUP_DC_PROXY", "")
                 or os.environ.get("TEEITUP_PROXY", ""))
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    pu = urllib.parse.urlparse(raw)
    return {"scheme": pu.scheme, "host": pu.hostname, "port": pu.port,
            "user": urllib.parse.unquote(pu.username or ""),
            "pw": urllib.parse.unquote(pu.password or "")}


def _proxies(pp: dict, variant: str, sid: str):
    """Build a proxies dict for a username variant + sticky session id."""
    user = pp["user"]
    base = user.split("-", 1)[0] if user else ""
    num = int(hashlib.md5(sid.encode()).hexdigest()[:8], 16) % 1000000
    if variant == "raw":
        u = user
    elif variant == "ws-us-sid":       # webshare residential country+sticky
        u = f"{base}-us-{num}"
    elif variant == "ws-sid":          # webshare sticky, no country (datacenter)
        u = f"{base}-{num}"
    elif variant == "sessid":          # dataimpulse
        u = f"{user};sessid.{num}"
    else:
        u = user
    auth = f"{urllib.parse.quote(u)}:{urllib.parse.quote(pp['pw'])}@" if u else ""
    server = f"{pp['scheme']}://{auth}{pp['host']}" + (f":{pp['port']}" if pp['port'] else "")
    return {"http": server, "https": server}


def _echo(proxies) -> tuple[bool, str]:
    try:
        s = requests.Session(); s.proxies.update(proxies)
        r = s.get(IPECHO, timeout=TIMEOUT)
        if r.status_code == 200:
            return True, r.json().get("ip", "?")
        return False, f"HTTP{r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"


def pick_variant(pp: dict):
    """Return (variant, note) for the first username form that authenticates.
    Prefers a form that also holds ONE IP across two calls (sticky)."""
    order = ["ws-us-sid", "ws-sid", "sessid", "raw"]
    working = None
    for v in order:
        # two echoes on the SAME logical sticky id -> do IPs match?
        pr = _proxies(pp, v, "probe-sid")
        ok1, ip1 = _echo(pr)
        ok2, ip2 = _echo(pr)
        note = f"{v}: echo1={ip1} echo2={ip2}"
        print("CPSDCP variant " + note + (" STICKY" if ok1 and ip1 == ip2 else
              (" ROTATES" if ok1 and ok2 else " FAIL")), flush=True)
        if ok1 and ok2 and working is None:
            working = (v, "sticky" if ip1 == ip2 else "rotating")
            if ip1 == ip2:
                return working  # sticky + working = ideal, stop early
    return working or (None, "none authenticated")


def probe_tenant(sub: str, date_str: str, pp: dict, variant: str) -> dict:
    base = f"https://{sub}.cps.golf"
    api = base + API_PATH
    proxies = _proxies(pp, variant, sub)  # per-tenant sticky id
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.proxies.update(proxies)
    r = {"sub": sub, "stage": "config", "status": 0, "teetimes": 0,
         "challenged": False, "auth": "none", "ip": ""}

    ok, ip = _echo(proxies)  # exit IP for this tenant's session
    r["ip"] = ip

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


CHALLENGED = [
    "cattailcreek", "flatironsgolf", "fossiltrace", "gypsumcreekgolf",
    "indiantree", "marianabutte", "oldecourseloveland", "universityofdenver",
    "sewailo", "westfields", "oldtrailgc", "williamsburgnatgc", "stonebrookfl",
    "eagleslanding", "manowar", "waradmiral", "rumpointe", "lighthousesound",
    "nutterscrossing", "glenmoor",
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="cps via sticky datacenter proxy (self-tuning)")
    p.add_argument("--date", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--only", default="", help="comma-separated subs (blank = the challenged set)")
    p.add_argument("--gap", type=float, default=0.3)
    a = p.parse_args(argv)

    pp = _parse_proxy()
    print(f"CPSDCP date={a.date} DC-proxy={'set host='+str(pp['host']) if pp else 'MISSING'}",
          flush=True)
    if not pp:
        print("CPSDCP no proxy secret — nothing to test", flush=True); return 0

    variant, kind = pick_variant(pp)
    print(f"CPSDCP chosen-variant={variant} ({kind})", flush=True)
    if not variant:
        print("CPSDCP proxy did not authenticate under any username form — "
              "check the secret / provider sticky syntax", flush=True); return 0

    have = set(_cps_subs(a.registry))
    if a.only:
        subs = [x.strip() for x in a.only.split(",") if x.strip()]
    else:
        subs = [s for s in CHALLENGED if s in have]

    ok = challenged = other = 0
    for sub in subs:
        r = probe_tenant(sub, a.date, pp, variant)
        tag = ("OK" if r["teetimes"] > 0 else
               "CHALLENGED" if r["challenged"] else "empty/fail")
        if r["teetimes"] > 0: ok += 1
        elif r["challenged"]: challenged += 1
        else: other += 1
        print("CPSDCP %-20s %-11s stage=%-8s status=%s tt=%d ip=%s%s"
              % (sub, tag, r["stage"], r["status"], r["teetimes"], r["ip"],
                 (" err=" + r["error"]) if r.get("error") else ""), flush=True)
        time.sleep(a.gap)
    print(f"CPSDCP DONE variant={variant}({kind}) ok={ok} challenged={challenged} "
          f"other={other} of {len(subs)} — OK count = tenants movable off residential",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
