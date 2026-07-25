"""Round-3 diagnostic: the Colorado courses that still capture nothing.

Round 2 (probe-results/diag2.txt) split the original "14 misses" into four
different problems and cleared six of them as stale slugs of mine. This script
goes after what is genuinely left, and it prints raw evidence rather than a
verdict so the fix can be written from fact:

  A. Quick18 — Homestead returns 18kB of real HTML and the parser yields 0 rows.
     Dump the table structure (header cells, first data rows, any "no times"
     banner) and compare against Thorncreek, which works. Also try the newer
     play18.com domain.

  B. Teesnap — hollydotgolf and petteyspark serve a Laravel 500 from
     /customer-api/teetimes-day while their homepages return 200. Show the
     homepage status, whether window.courses is present, and the exact body the
     API returns, so we can tell "dead tenant" from "changed route".

  C. Club Prophet — universityofdenver and emeraldgreens 404 on
     /identityapi/myconnect/token/short. Try the other token routes a CPS v4
     site is known to expose, against a working tenant as the control.

  D. Lake Arbor has no cps.golf tenant at all; its portal is
     secure.west.prophetservices.com. Probe what that host actually serves.

  E. TeeItUp — rollingstone-ranch and golf-granby-ranch resolve to a facility
     but return 0 slots. Print the raw /v2/tee-times payload across a week to
     separate "genuinely nothing bookable" from a parse/filter bug.

Nothing here needs credentials; every request is one a browser makes when you
open the course's public booking page.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "*/*"})

import zoneinfo  # noqa: E402
TODAY = dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    sys.stdout.flush()


def show(label: str, r) -> None:
    ct = r.headers.get("content-type", "")
    print(f"   {label}: HTTP {r.status_code} {len(r.content)}B  {ct[:40]}")


# ---------------------------------------------------------------- A. Quick18
def quick18() -> None:
    hr("A. QUICK18 — Homestead parses to 0 rows; Thorncreek is the control")
    from bs4 import BeautifulSoup
    for sub, domain in (("homestead", "quick18.com"),
                        ("homestead", "play18.com"),
                        ("thorncreek", "quick18.com")):
        for d in (TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=6)):
            url = f"https://{sub}.{domain}/teetimes/searchmatrix"
            try:
                r = S.get(url, params={"teedate": d.strftime("%Y%m%d")},
                          timeout=25)
            except Exception as exc:  # noqa: BLE001
                print(f"   {sub}.{domain} {d}: EXC {type(exc).__name__}: {exc}")
                continue
            show(f"{sub}.{domain} {d}", r)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            trs = soup.find_all("tr")
            print(f"      tables={len(soup.find_all('table'))} rows={len(trs)}")
            # Anything that looks like a "no times" message
            low = r.text.lower()
            for phrase in ("no tee times", "not available", "no times",
                           "closed", "sold out", "please select",
                           "no results"):
                if phrase in low:
                    i = low.find(phrase)
                    print(f"      banner {phrase!r}: "
                          f"...{r.text[max(0,i-90):i+90]!r}")
                    break
            for tr in trs[:6]:
                cells = [c.get_text(' ', strip=True)[:26]
                         for c in tr.find_all(['th', 'td'])]
                if cells:
                    print(f"      row: {cells}")
            # Does the page instead drive an XHR? list script srcs / fetch urls
            for kw in ("searchmatrix", "/teetimes/", "api/"):
                pass


# ---------------------------------------------------------------- B. Teesnap
def teesnap() -> None:
    hr("B. TEESNAP — hollydot / petteys park 500 on the tee-sheet API")
    date = (TODAY + dt.timedelta(days=1)).isoformat()
    for sub in ("hollydotgolf", "petteyspark", "coyotecreek", "meadowhills"):
        print(f"\n--- {sub}.teesnap.net")
        try:
            r = S.get(f"https://{sub}.teesnap.net/", timeout=25)
        except Exception as exc:  # noqa: BLE001
            print(f"   homepage EXC {type(exc).__name__}: {exc}")
            continue
        show("homepage", r)
        html = r.text
        i = html.find("window.courses")
        print(f"   window.courses present: {i >= 0}")
        if i >= 0:
            print(f"      {html[i:i+220]!r}")
        # Redirected somewhere else entirely? (tenant migrated)
        if r.url.rstrip("/") != f"https://{sub}.teesnap.net":
            print(f"   REDIRECTED to {r.url}")
        for path in ("/customer-api/teetimes-day",
                     "/customer-api/courses",
                     "/api/teetimes-day"):
            try:
                rr = S.get(f"https://{sub}.teesnap.net{path}",
                           params={"date": date}, timeout=25)
            except Exception as exc:  # noqa: BLE001
                print(f"   {path}: EXC {type(exc).__name__}")
                continue
            show(path, rr)
            print(f"      body[:200]={rr.text[:200]!r}")


# ----------------------------------------------------------- C. Club Prophet
def clubprophet() -> None:
    hr("C. CLUB PROPHET — token route 404s for two tenants")
    routes = [
        "/identityapi/myconnect/token/short",
        "/identityapi/connect/token",
        "/identityapi/myconnect/token",
        "/onlineres/api/token",
        "/onlineres/",
        "/",
    ]
    for tenant in ("universityofdenver", "emeraldgreens", "eagletrace",
                   "indianpeaks", "flatirons"):
        print(f"\n--- {tenant}.cps.golf")
        for path in routes:
            url = f"https://{tenant}.cps.golf{path}"
            try:
                r = (S.post(url, timeout=20) if "token" in path
                     else S.get(url, timeout=20))
            except Exception as exc:  # noqa: BLE001
                print(f"   {path}: EXC {type(exc).__name__}")
                continue
            show(path, r)
            if path == "/" and r.status_code == 200:
                t = r.text
                for marker in ("<title>", "Club Prophet", "no-online-booking",
                               "not currently", "coming soon"):
                    j = t.find(marker)
                    if j >= 0:
                        print(f"      {marker!r}: {t[j:j+110]!r}")


# ------------------------------------------------------------- D. Lake Arbor
def lake_arbor() -> None:
    hr("D. LAKE ARBOR — portal is prophetservices, not cps.golf")
    for url in ("https://secure.west.prophetservices.com/LakeArborGC/",
                "https://secure.west.prophetservices.com/LakeArborGC/Account/nLogon",
                "https://lakearbor.cps.golf/"):
        try:
            r = S.get(url, timeout=25, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            print(f"   {url}: EXC {type(exc).__name__}: {exc}")
            continue
        show(url, r)
        print(f"      final={r.url}")
        t = r.text
        j = t.lower().find("<title>")
        if j >= 0:
            print(f"      title={t[j:j+110]!r}")
        # Does it need a login to see the sheet at all?
        for marker in ("password", "Sign In", "nLogon", "Guest"):
            if marker.lower() in t.lower():
                print(f"      mentions {marker!r}")


# ---------------------------------------------------------------- E. TeeItUp
def teeitup() -> None:
    hr("E. TEEITUP — rollingstone / granby ranch resolve but return 0 slots")
    from scraper.adapters.teeitup import TeeItUpAdapter, API_BASE
    a = TeeItUpAdapter()
    for alias in ("rollingstone-ranch", "granby-ranch", "golf-granby-ranch",
                  "indian-peaks-golf-course"):
        print(f"\n--- alias {alias}")
        try:
            facs = a.discover_facilities(alias)
            print(f"   facilities: {[(f.get('id'), f.get('name')) for f in facs]}")
        except Exception as exc:  # noqa: BLE001
            print(f"   facilities EXC {type(exc).__name__}: {exc}")
            continue
        if not facs:
            continue
        for d in range(1, 8):
            date = (TODAY + dt.timedelta(days=d)).isoformat()
            try:
                r = S.get(f"{API_BASE}/v2/tee-times",
                          params={"date": date},
                          headers={"x-be-alias": alias}, timeout=25)
            except Exception as exc:  # noqa: BLE001
                print(f"   {date}: EXC {type(exc).__name__}")
                continue
            if r.status_code != 200:
                print(f"   {date}: HTTP {r.status_code}")
                continue
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                print(f"   {date}: non-JSON {r.text[:80]!r}")
                continue
            if isinstance(data, list):
                total = sum(len(x.get("teetimes") or []) for x in data
                            if isinstance(x, dict))
                names = [x.get("courseName") or x.get("facilityId")
                         for x in data if isinstance(x, dict)]
                print(f"   {date}: {len(data)} facility blocks, "
                      f"{total} raw teetimes, {names}")
                if total and d <= 2:
                    blk = next(x for x in data if x.get("teetimes"))
                    print("      sample teetime: "
                          + json.dumps(blk["teetimes"][0])[:300])
            else:
                print(f"   {date}: {type(data).__name__} {str(data)[:160]}")


def main() -> None:
    print(f"diag_misses — Denver today {TODAY}")
    for fn in (quick18, teesnap, clubprophet, lake_arbor, teeitup):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"\n!! {fn.__name__} blew up:")
            traceback.print_exc(file=sys.stdout)
    print("\ndone")


if __name__ == "__main__":
    main()
