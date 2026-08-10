"""PROBE: can we reach teesnap.net from the DATACENTER runner, and via what?

teesnap resets connections from datacenter IPs (adapters/teesnap.py documents
ConnectionResetError 104 with a retry that "almost always succeeds"). As of Aug
2026 all ~25 teesnap courses are DARK nationwide => the retries now always fail =>
teesnap tightened its datacenter blocking. This probe finds the CHEAPEST fix by
testing three transports from the free GitHub runner:

  * plain  requests           — baseline (expect reset/fail)
  * curl_cffi impersonate      — if this clears it, the reset is TLS-FINGERPRINT
                                 based and the fix is FREE (no proxy).
  * rotating DC proxy          — if only this clears it, the block is IP-based and
    (TEEITUP_DC_PROXY)           we route teesnap through the (cheap) proxy.

Per subdomain per method: fetch the homepage (window.courses => a course id) and
the teetimes-day JSON. Reports home status + tee-time slots. NO D1 push.

Usage: python -m scraper.probe_teesnap --date 2026-08-11
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys

log = logging.getLogger("teetime")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

SUBS = ["heathergardens", "golfpagosa", "sundancegolfclub", "lakehavasu",
        "kindertoncc", "mtmassivegolf", "petteyspark", "stoneridgegc"]


def _course_id(html: str):
    m = re.search(r"window\.courses\s*=\s*\[\s*\{", html)
    if not m:
        return None
    seg = html[m.end():m.end() + 600]
    mm = re.search(r'"id"\s*:\s*(\d+)', seg)
    return mm.group(1) if mm else None


def _tt_url(sub: str, cid: str, date: str) -> str:
    return (f"https://{sub}.teesnap.net/customer-api/teetimes-day"
            f"?course={cid}&date={date}&players=1&holes=18&addons=off")


def _slots(text: str):
    try:
        j = json.loads(text)
        return len((j.get("teeTimes") or {}).get("teeTimes") or [])
    except Exception:  # noqa: BLE001
        return "parse_fail"


def _run(sub: str, date: str, session) -> dict:
    """session must have .get(url, timeout=). Returns a compact record."""
    rec: dict = {}
    try:
        r = session.get(f"https://{sub}.teesnap.net/", timeout=20)
        rec["home"] = getattr(r, "status_code", "?")
        rec["home_bytes"] = len(r.text)
        cid = _course_id(r.text)
        rec["cid"] = cid
        if cid:
            tt = session.get(_tt_url(sub, cid, date), timeout=20)
            rec["tt"] = getattr(tt, "status_code", "?")
            rec["slots"] = _slots(tt.text)
    except Exception as e:  # noqa: BLE001 — connection resets land here
        rec["err"] = f"{type(e).__name__}: {str(e)[:70]}"
    return rec


def _plain_session(proxies=None):
    import requests
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "en-US,en;q=0.9"})
    if proxies:
        s.proxies.update(proxies)
    return s


def _cffi_session():
    from curl_cffi import requests as creq
    return creq.Session(impersonate="chrome")


def _proxies():
    raw = os.environ.get("TEEITUP_DC_PROXY", "").strip()
    if not raw:
        return None
    raw = re.sub(r"\s+", "", raw)
    if "://" not in raw:
        raw = "http://" + raw
    return {"http": raw, "https": raw}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="teesnap transport probe")
    p.add_argument("--date", default="2026-08-11")
    p.add_argument("--subs", default=",".join(SUBS))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    subs = [x.strip() for x in a.subs.split(",") if x.strip()]
    proxies = _proxies()

    have_cffi = True
    try:
        import curl_cffi  # noqa: F401
    except Exception:  # noqa: BLE001
        have_cffi = False

    out: dict = {}
    for sub in subs:
        rec = {"plain": _run(sub, a.date, _plain_session())}
        rec["cffi"] = _run(sub, a.date, _cffi_session()) if have_cffi else {"err": "no curl_cffi"}
        rec["dcproxy"] = _run(sub, a.date, _plain_session(proxies)) if proxies else {"err": "no proxy secret"}
        out[sub] = rec
        log.info("%-18s %s", sub, json.dumps(rec))

    print("\n===== TEESNAP METHOD PROBE (date " + a.date + ") =====")
    print(json.dumps(out, indent=2))
    for method in ["plain", "cffi", "dcproxy"]:
        home200 = [s for s in subs if out[s][method].get("home") == 200]
        won = [s for s in subs if isinstance(out[s][method].get("slots"), int)
               and out[s][method]["slots"] > 0]
        print(f"{method:8s}: home-200 {len(home200)}/{len(subs)}, "
              f"slots>0 {len(won)}/{len(subs)}  ({', '.join(won) or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
