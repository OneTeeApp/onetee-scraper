"""Probe: WHY does golfrev.com 403 the datacenter runner, and what's the
cheapest fix? Birch Creek's tee sheet (golfrev/Cybergolf) serves fine to a
residential browser but returned HTTP 403 (Cloudflare block page) to plain
python-requests from GitHub Actions. Before assuming a residential proxy, walk
the cheap ladder and see where it clears:

  1. plain requests + our stock Mozilla UA           (baseline — expect 403)
  2. plain requests + a FULL Chrome header set        (is it header-based?)
  3. curl_cffi impersonate=chrome, NO proxy           (is it TLS/JA3 fingerprint?)
  4. curl_cffi impersonate=chrome + full headers, NO proxy
  5. curl_cffi impersonate=chrome via TEEITUP_DC_PROXY (datacenter proxy — regular)
  6. curl_cffi impersonate=chrome via TEEITUP_PROXY    (residential — last resort)

Read-only. Prints a table of status / bytes / card-count per method for a couple
near dates that we KNOW have inventory (verified 8/9 → 6 cards from a browser).
cards>0 on any row = that method clears Cloudflare from the datacenter.
"""
import datetime as dt
import os
import re

import requests

try:
    from curl_cffi import requests as creq
except Exception as e:  # noqa: BLE001
    creq = None
    print("curl_cffi import failed:", e, flush=True)

URL = "https://www.golfrev.com/go/tee_times/teetime_table_html.asp"
COURSEID = "3719"
HTC = "370"

STOCK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FULL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{URL.rsplit('/', 1)[0]}/?htc={HTC}&courseid={COURSEID}&r=1",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}


def _params(date: dt.date) -> dict:
    return {"c": COURSEID, "s": f"{date.month}/{date.day}/{date.year}",
            "h": HTC, "specials": "", "reset": "yes", "snapshot": "no"}


def _report(r) -> str:
    if r is None:
        return "None"
    body = r.text or ""
    cards = len(body.split("showBooking(")) - 1
    cf = ""
    try:
        cf = r.headers.get("cf-ray", "")[:14]
    except Exception:  # noqa: BLE001
        pass
    return f"HTTP{r.status_code} bytes={len(body):<5} cards={cards:<3} cf-ray={cf}"


def run(label, getter, date):
    try:
        r = getter(date)
        return _report(r)
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {str(e)[:120]}"


def main():
    dc = re.sub(r"\s+", "", os.environ.get("TEEITUP_DC_PROXY", "").strip())
    res = re.sub(r"\s+", "", os.environ.get("TEEITUP_PROXY", "").strip())
    dates = [dt.date.today() + dt.timedelta(days=n) for n in (1, 2)]

    def plain_stock(date):
        return requests.get(URL, params=_params(date),
                            headers={"User-Agent": STOCK_UA}, timeout=25)

    def plain_full(date):
        return requests.get(URL, params=_params(date),
                            headers=FULL_HEADERS, timeout=25)

    def imp_bare(date):
        return creq.get(URL, params=_params(date), impersonate="chrome",
                        timeout=25)

    def imp_full(date):
        return creq.get(URL, params=_params(date), headers=FULL_HEADERS,
                        impersonate="chrome", timeout=25)

    def imp_dc(date):
        return creq.get(URL, params=_params(date), impersonate="chrome",
                        proxies={"http": dc, "https": dc}, timeout=30)

    def imp_res(date):
        return creq.get(URL, params=_params(date), impersonate="chrome",
                        proxies={"http": res, "https": res}, timeout=30)

    methods = [
        ("1 plain + stock UA        ", plain_stock, True),
        ("2 plain + full Chrome hdrs ", plain_full, True),
        ("3 curl_cffi impersonate    ", imp_bare, creq is not None),
        ("4 curl_cffi imp + full hdrs ", imp_full, creq is not None),
        ("5 curl_cffi imp + DC proxy  ", imp_dc, creq is not None and bool(dc)),
        ("6 curl_cffi imp + RES proxy ", imp_res, creq is not None and bool(res)),
    ]

    print(f"golfrev probe — courseid={COURSEID} htc={HTC}", flush=True)
    print(f"DC proxy set: {bool(dc)}   RES proxy set: {bool(res)}   "
          f"curl_cffi: {creq is not None}\n", flush=True)
    for label, fn, enabled in methods:
        if not enabled:
            print(f"  {label}  (skipped — not available)", flush=True)
            continue
        cells = "   ".join(f"{d.isoformat()} {run(label, fn, d)}" for d in dates)
        print(f"  {label}  {cells}", flush=True)


if __name__ == "__main__":
    main()
