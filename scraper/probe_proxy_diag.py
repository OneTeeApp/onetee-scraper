"""One-shot proxy diagnostic — pinpoints why plain HTTP through TEEITUP_PROXY
fails. Read-only, no D1. Prints the FULL exception (the production probes
truncate to 120 chars, hiding the root cause).

It runs two GETs through the proxy and separates the two failure worlds:
  1. a neutral IP-echo (api.ipify.org) — does the proxy work AT ALL?
  2. kenna facilities — does kenna accept the proxy's exit IP?

Verdict logic:
  ipecho OK + kenna OK      → proxy fine, kenna fine (prod issue was elsewhere).
  ipecho OK + kenna FAIL    → proxy works; KENNA blocks the proxy's exit IPs
                              (Webshare datacenter IPs are flagged) — bad news,
                              no config fix; need a different IP source.
  ipecho FAIL (407/Proxy)   → the PROXY itself rejects us: wrong endpoint or
                              auth mode. Fix on Webshare (enable username/
                              password auth; use the ROTATING endpoint).

Credentials are NEVER printed — only scheme://host:port is shown.
"""
from __future__ import annotations

import os
import re
import urllib.parse

import requests

KENNA = "https://phx-api-be-east-1b.kenna.io/alias/coldwater-golf-club/facilities"
IPECHO = "https://api.ipify.org?format=json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _masked(raw: str) -> str:
    """scheme://host:port only — drop any user:pass so no secret is logged."""
    try:
        u = urllib.parse.urlparse(raw)
        host = u.hostname or "?"
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://{host}{port}"
    except Exception:  # noqa: BLE001
        return "(unparseable)"


def _try(session, url, label):
    try:
        r = session.get(url, timeout=25, headers={"User-Agent": UA})
        body = (r.text or "")[:160].replace("\n", " ")
        print(f"DIAG {label}: status={r.status_code} bytes={len(r.text)} body={body!r}",
              flush=True)
        return r.status_code
    except Exception as e:  # noqa: BLE001 — the WHOLE point is to see the full error
        print(f"DIAG {label}: EXC {type(e).__name__}: {str(e)[:400]}", flush=True)
        return None


def main() -> int:
    raw = re.sub(r"\s+", "", os.environ.get("TEEITUP_PROXY", "").strip())
    if not raw:
        print("DIAG no TEEITUP_PROXY set — nothing to test", flush=True)
        return 0
    print(f"DIAG proxy={_masked(raw)} (creds hidden)", flush=True)
    proxies = {"http": raw, "https": raw}

    s = requests.Session()
    s.proxies.update(proxies)
    print("DIAG --- through the proxy ---", flush=True)
    ip_ok = _try(s, IPECHO, "ipecho-via-proxy")
    kenna_ok = _try(s, KENNA + "", "kenna-via-proxy")
    # add the x-be-alias header kenna needs (retry kenna properly)
    try:
        r = s.get(KENNA, timeout=25,
                  headers={"User-Agent": UA, "x-be-alias": "coldwater-golf-club"})
        print(f"DIAG kenna-via-proxy(+hdr): status={r.status_code} bytes={len(r.text)}",
              flush=True)
        kenna_ok = r.status_code
    except Exception as e:  # noqa: BLE001
        print(f"DIAG kenna-via-proxy(+hdr): EXC {type(e).__name__}: {str(e)[:400]}",
              flush=True)
        kenna_ok = None

    print("DIAG --- verdict ---", flush=True)
    if ip_ok and kenna_ok:
        print("DIAG VERDICT: proxy OK + kenna OK — the prod failure was elsewhere", flush=True)
    elif ip_ok and not kenna_ok:
        print("DIAG VERDICT: PROXY WORKS but KENNA blocks the proxy exit IP — "
              "Webshare datacenter IPs are flagged; config won't fix it", flush=True)
    elif not ip_ok:
        print("DIAG VERDICT: PROXY ITSELF FAILS (even to a neutral site) — wrong "
              "endpoint or auth. On Webshare: use the ROTATING endpoint and enable "
              "username/password auth (not IP-authorization)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
