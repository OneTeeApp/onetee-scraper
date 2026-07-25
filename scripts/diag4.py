"""Round-4 diagnostic for the remaining Colorado capture misses.

Round 3 (probe-results/diag3.txt) answered two of the five questions and
mis-asked the other two. What it settled:

  * Lake Arbor's portal is secure.west.prophetservices.com and every route
    redirects to a Log On page -> login-gated, off limits. Nothing to fix.
  * universityofdenver / emeraldgreens / eagletrace .cps.golf answer 404 on
    /identityapi/myconnect/token/short while indianpeaks (a working control)
    answers 400. A 404 means the identity app is not deployed on that host at
    all, so those three are probably not real CPS tenants. Confirming that is
    a website-scan job, not an API job -> scripts/native_probe.py.

What it mis-asked, and what this script asks instead:

  B. TEESNAP. Round 3 hit /customer-api/teetimes-day with NO query string, so
     the SPA catch-all returned the HTML shell for every tenant including the
     control. Useless. Here we call the exact URL the adapter calls, with the
     course id read out of window.courses, against two known-good tenants and
     the two suspects.

  E. TEEITUP. Round 3's control (indian-peaks-golf-course) was a bad choice —
     Indian Peaks is a Club Prophet course, its teeitup alias is a leftover, so
     "0 slots everywhere" proved nothing. Here the controls are two aliases we
     actively publish from (buffalo-run, commonground).

  A. QUICK18. Homestead returned a well-formed matrix with zero data rows on
     both probed dates while Thorncreek returned 12-15. That reads as a real
     empty sheet rather than a parser bug, but two dates is thin — sweep seven.

Public pages only: no credentials, no CAPTCHA solving, no TLS forgery.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import zoneinfo

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

DEN = zoneinfo.ZoneInfo("America/Denver")
TODAY = dt.datetime.now(DEN).date()
KENNA = "https://phx-api-be-east-1b.kenna.io"


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def get(url: str, **kw):
    kw.setdefault("timeout", 25)
    try:
        return S.get(url, **kw)
    except Exception as exc:  # noqa: BLE001
        print(f"   EXC {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- A. quick18
def quick18() -> None:
    rule("A. QUICK18 — is Homestead's sheet actually empty? (7 days, control)")
    for sub in ("homestead", "thorncreek"):
        for d in (TODAY + dt.timedelta(days=n) for n in range(1, 8)):
            url = (f"https://{sub}.quick18.com/teetimes/searchmatrix"
                   f"?teedate={d:%Y%m%d}")
            r = get(url)
            if r is None:
                continue
            html = r.text
            # data rows carry a time cell like "7:10 AM" in the matrix body
            times = re.findall(r">\s*(\d{1,2}:\d{2}\s*[AP]M)\s*<", html)
            msgs = sorted(set(re.findall(
                r'class="mtrx[A-Za-z]*Message"[^>]*>([^<]{3,80})<', html)))
            no_times = "no tee times" in html.lower()
            print(f"   {sub} {d}: HTTP {r.status_code} {len(html)}B "
                  f"| {len(times)} time cells | no-tee-times banner={no_times}")
            if msgs:
                print("      messages:", msgs[:4])
            if times:
                print("      first/last:", times[0], "..", times[-1])


# ---------------------------------------------------------------- B. teesnap
def teesnap() -> None:
    rule("B. TEESNAP — the real adapter call, with course id and params")
    subs = [("golfpagosa", "CONTROL"), ("mtmassivegolf", "CONTROL"),
            ("hollydotgolf", "suspect"), ("petteyspark", "suspect")]
    for sub, kind in subs:
        print(f"\n--- {sub}.teesnap.net  ({kind})")
        r = get(f"https://{sub}.teesnap.net/")
        if r is None:
            continue
        html = r.text
        start = html.find("window.courses")
        region = html[start:start + 30000] if start >= 0 else html
        ids: list[str] = []
        for i in re.findall(r'"id":\s*(\d+)\s*,\s*"created_at"', region):
            if i not in ids:
                ids.append(i)
        names = re.findall(r'"name":"([^"]{2,60})"', region)[:6]
        print(f"   homepage HTTP {r.status_code} {len(html)}B "
              f"| course ids {ids} | names {names}")
        for cid in ids[:3]:
            for d in (TODAY + dt.timedelta(days=n) for n in (1, 3)):
                for holes in (18, 9):
                    u = (f"https://{sub}.teesnap.net/customer-api/teetimes-day"
                         f"?course={cid}&date={d}&players=1&holes={holes}"
                         f"&addons=off")
                    rr = get(u)
                    if rr is None:
                        continue
                    ct = rr.headers.get("content-type", "")
                    tag = f"   id={cid} {d} holes={holes}: HTTP {rr.status_code} {len(rr.content)}B {ct}"
                    if "json" in ct:
                        try:
                            blk = (rr.json() or {}).get("teeTimes", {})
                            slots = blk.get("teeTimes", []) or []
                            print(tag, f"| {len(slots)} slots")
                            if slots:
                                print("      slot[0]:",
                                      json.dumps(slots[0])[:260])
                        except Exception as exc:  # noqa: BLE001
                            print(tag, f"| JSON parse failed: {exc}")
                    else:
                        print(tag, "| NOT JSON:",
                              rr.text[:120].replace("\n", " "))


# --------------------------------------------------------------- E. tee it up
def teeitup() -> None:
    rule("E. TEEITUP — controls we actually publish from vs the two suspects")
    aliases = [("buffalo-run-golf-course", "CONTROL"),
               ("commonground-golf-course", "CONTROL"),
               ("golf-granby-ranch", "suspect"),
               ("rollingstone-ranch", "suspect")]
    for alias, kind in aliases:
        print(f"\n--- {alias}  ({kind})")
        h = {"x-be-alias": alias}
        fac_ids: list[str] = []
        for route in (f"{KENNA}/v2/courses", f"{KENNA}/alias/{alias}/facilities"):
            r = get(route, headers=h)
            if r is None:
                continue
            print(f"   {route.split(KENNA)[1]}: HTTP {r.status_code} "
                  f"{len(r.content)}B")
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                continue
            items = data if isinstance(data, list) else data.get("courses", [])
            for f in items:
                cid = f.get("courseId") or f.get("id")
                if cid is not None and str(cid) not in fac_ids:
                    fac_ids.append(str(cid))
                print(f"      id={cid} name={f.get('name')!r} "
                      f"tz={f.get('timeZone')!r}")
            if fac_ids:
                break
        for d in (TODAY + dt.timedelta(days=n) for n in (1, 3, 6)):
            for params in ({"date": str(d)},
                           {"date": str(d), "facilityIds": ",".join(fac_ids)}):
                if "facilityIds" in params and not fac_ids:
                    continue
                r = get(f"{KENNA}/v2/tee-times", headers=h, params=params)
                if r is None:
                    continue
                n = 0
                blocks = 0
                try:
                    data = r.json()
                    blocks = len(data) if isinstance(data, list) else 1
                    for b in (data if isinstance(data, list) else [data]):
                        n += len(b.get("teetimes") or [])
                except Exception:  # noqa: BLE001
                    pass
                key = "facilityIds" if "facilityIds" in params else "date-only"
                print(f"   {d} {key:11s}: HTTP {r.status_code} "
                      f"{len(r.content)}B | {blocks} blocks | {n} teetimes")
                if r.status_code == 200 and n == 0 and len(r.content) < 900:
                    print("      body:", r.text[:220].replace("\n", " "))


if __name__ == "__main__":
    print(f"diag4 — Denver today {TODAY}")
    for fn in (quick18, teesnap, teeitup):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — one section must not kill the rest
            print(f"\n!! {fn.__name__} blew up: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    print("\ndone")
