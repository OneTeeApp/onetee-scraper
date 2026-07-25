"""Round 2 of the sub-course / capture-miss diagnosis.

Round 1 (probe-results/diag.txt) established:

  * MemberSports has no readable club-listing endpoint (401/404 on all four
    obvious shapes) BUT `golfCourseId: 0` on the tee-sheet POST means "every
    course", which is how sub-courses can be enumerated.
  * With configurationTypeId 0, club 3697 returns only Foothills sheets and
    club 3821 returns only Tiara Rado — so `meadows-golf-club` and
    `lincoln-park-golf-course` are currently serving a neighbour's tee sheet.
    Club 3724 returned nothing at all, and Kennedy (3629) returned only its
    Par 3 sheet.
  * TeeItUp/Hyland Hills is healthy: 3 facilities, labels populate.
  * Six of the "14 CO misses" were my own stale slugs — the courses ARE in the
    registry under different slugs.

The registry's booking URLs are the clue for part A. They look like
`/tee-times/{clubId}/{courseId}/{groupId}/{configurationTypeId}/{sheetType}`
and the last-but-one segment is NOT always 0:

    foothills   /tee-times/3697/4758/0/3/0
    lincoln pk  /tee-times/3821/4918/0/4/0
    boomerang   /tee-times/3724/4793/0/5/0
    fox hollow  /tee-times/3703/20589/0/7/0

The adapter hardcodes configurationTypeId 0 for everything. If that segment is
the configuration type, sweeping it should reveal the sheets that are currently
invisible. Separately, six Denver municipals point at
`/book-linked-clubs-tee-time/3660/4711/1` or `/custom/3660/4711/19`, i.e. a
linked-club portal — so a club-group query may return several clubs at once.

Parts:
  A1. Enumerate the SPA's own API routes out of its JS bundle.
  A2. Sweep configurationTypeId x golfClubGroupId per club with golfCourseId=0.
  B.  Re-run the six mis-slugged CO courses under their real registry slugs.
  C.  Get the actual failure detail for the four genuinely-broken adapters.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

import requests

sys.path.insert(0, os.getcwd())

from scraper.aggregate import ADAPTERS  # noqa: E402

MS_API = "https://api.membersports.com/api/v1"
MS_KEY = os.environ.get("MEMBERSPORTS_API_KEY",
                        "A9814038-9E19-4683-B171-5A06B39147FC")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MS_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-api-key": MS_KEY,
    "Origin": "https://app.membersports.com",
    "Referer": "https://app.membersports.com/",
    "User-Agent": UA,
}

# club_id -> (label, configurationTypeId seen in the registry's booking URL)
CLUBS = {
    3629: ("Kennedy (Aurora)", 19),
    3724: ("Boomerang / Highland Hills (Greeley)", 5),
    3697: ("Foothills / Meadows", 3),
    3821: ("Lincoln Park / Tiara Rado (Grand Junction)", 4),
    3660: ("City Park / Denver linked clubs", 1),
    3703: ("Fox Hollow (control, cfg 7)", 7),
}

# The six that round 1 reported as "NOT IN REGISTRY" — my slugs were stale.
RESLUG = [
    "emerald-greens-golf-club",
    "lake-arbor-golf-club",
    "clubcorp-at-black-bear-golf-club",
    "desert-hawk-at-pueblo-west",
    "golf-granby-ranch",
    "the-course-at-petteys-park",
]

# Genuinely broken in round 1 (excluding the three GolfNow ones, which are
# expected to fail on the plain path and are served by the browser fetcher).
BROKEN = [
    "university-of-denver-golf-club-at-highlands-ranch",  # cps 404
    "homestead-golf-course",                              # quick18 0 slots
    "rollingstone-ranch-golf-club",                       # teeitup 0 slots
    "coyote-creek-golf-course",                           # teesnap 500
    "hollydot-golf-course",                               # teesnap 500
]


def hr(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def probe_spa_routes() -> None:
    hr("A1. MEMBERSPORTS SPA — API ROUTES IN THE JS BUNDLE")
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    try:
        r = s.get("https://app.membersports.com/", timeout=25)
        print("   GET app.membersports.com ->", r.status_code, len(r.text), "bytes")
        srcs = re.findall(r'src="([^"]+\.js)"', r.text)
        print("   scripts:", srcs[:12])
    except Exception as e:  # noqa: BLE001
        print("   index EXC:", e)
        return

    routes: set[str] = set()
    for src in srcs[:12]:
        url = src if src.startswith("http") else \
            "https://app.membersports.com/" + src.lstrip("/")
        try:
            j = s.get(url, timeout=40)
            if j.status_code != 200:
                print(f"   {url} -> {j.status_code}")
                continue
            found = set(re.findall(r'["\'`](?:/)?(api/v\d+/[A-Za-z0-9/_{}$.-]+)',
                                   j.text))
            routes |= found
            print(f"   {url.rsplit('/', 1)[-1]}: {len(j.text)} bytes, "
                  f"{len(found)} route strings")
        except Exception as e:  # noqa: BLE001
            print(f"   {url} EXC: {e}")

    interesting = sorted(r for r in routes
                         if re.search(r"club|course|linked|group|booking",
                                      r, re.I))
    print(f"\n   {len(routes)} distinct api routes; "
          f"{len(interesting)} club/course-ish:")
    for r_ in interesting[:80]:
        print("     ", r_)


def sheet(club: int, date: dt.date, cfg: int, grp: int) -> tuple[int, dict]:
    body = {"configurationTypeId": cfg, "date": date.isoformat(),
            "golfClubGroupId": grp, "golfClubId": club,
            "golfCourseId": 0, "groupSheetTypeId": 0}
    r = requests.post(f"{MS_API}/golfclubs/onlineBookingTeeTimes",
                      json=body, headers=MS_HEADERS, timeout=25)
    if r.status_code != 200:
        return -r.status_code, {}
    data = r.json()
    if not isinstance(data, list):
        return -1, {}
    seen: dict = {}
    for row in data:
        for it in row.get("items", []):
            cid = it.get("golfCourseId")
            if cid is not None:
                seen.setdefault(cid, it.get("name") or "")
    return len(data), seen


def probe_config_sweep(date: dt.date) -> None:
    hr("A2. MEMBERSPORTS — configurationTypeId x golfClubGroupId SWEEP "
       "(golfCourseId=0)")
    print("   -N in the rows column means HTTP N.\n")
    for club, (label, hint) in CLUBS.items():
        print(f"--- club {club}: {label}  (registry URL hints cfg={hint})")
        union: dict = {}
        for cfg in (0, 1, 2, 3, 4, 5, 6, 7, hint, 19, 26):
            for grp in (0, 1):
                try:
                    n, seen = sheet(club, date, cfg, grp)
                except Exception as e:  # noqa: BLE001
                    print(f"   cfg={cfg} grp={grp} -> EXC {e}")
                    continue
                if n > 0 and seen:
                    union.update(seen)
                if n != 0 or seen:
                    print(f"   cfg={cfg:>2} grp={grp} -> {n:>4} rows  {seen}")
        print(f"   UNION of course ids for club {club}: {union}\n")


def run_slugs(title: str, slugs: list[str], date: dt.date) -> None:
    hr(title)
    reg = {c["slug"]: c for c in json.load(open("registry.json"))["courses"]}
    for slug in slugs:
        c = reg.get(slug)
        if not c:
            print(f"\n--- {slug}: STILL NOT IN REGISTRY")
            continue
        plat = c.get("platform")
        print(f"\n--- {slug} [{plat}] ids={c.get('ids')}")
        print(f"    url: {c.get('booking_url')}  city={c.get('city')} "
              f"{c.get('state')}")
        ad_cls = ADAPTERS.get(plat)
        if not ad_cls:
            print("    no adapter registered for this platform "
                  "(browser-only path?)")
            continue
        ad = ad_cls()
        for d in (date, date + dt.timedelta(days=1)):
            try:
                tts = ad.fetch(c, d)
                print(f"    {d}: {len(tts)} slots"
                      + (f" | first {tts[0].teetime}" if tts else " (EMPTY)"))
            except Exception as e:  # noqa: BLE001
                msg = str(e).replace("\n", " ")[:300]
                print(f"    {d}: EXC {type(e).__name__}: {msg}")


def probe_broken_detail(date: dt.date) -> None:
    hr("C2. FAILURE DETAIL FOR THE FOUR BROKEN ADAPTER CASES")
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    print("\n-- clubprophet: is universityofdenver.cps.golf still a CPS tenant?")
    for url in ("https://universityofdenver.cps.golf/",
                "https://universityofdenver.cps.golf/onlineresweb/search-teetime",
                "https://universityofdenver.cps.golf/identityapi/myconnect/"
                "token/short"):
        try:
            r = s.get(url, timeout=25, allow_redirects=True)
            print(f"   {url} -> {r.status_code} final={r.url} "
                  f"len={len(r.text)}")
            if r.status_code == 200:
                title = re.search(r"<title[^>]*>(.*?)</title>", r.text,
                                  re.S | re.I)
                print("     title:", (title.group(1).strip()[:120]
                                      if title else "(none)"))
        except Exception as e:  # noqa: BLE001
            print(f"   {url} -> EXC {e}")

    print("\n-- teesnap: raw response for the two 500s")
    for sub in ("coyotecreek", "hollydotgolf"):
        try:
            r = s.get(f"https://{sub}.teesnap.net/", timeout=25)
            print(f"   GET https://{sub}.teesnap.net/ -> {r.status_code} "
                  f"len={len(r.text)} has_window_courses="
                  f"{'window.courses' in r.text}")
        except Exception as e:  # noqa: BLE001
            print(f"   GET https://{sub}.teesnap.net/ -> EXC {e}")
        for cid in (1, 2):
            u = (f"https://{sub}.teesnap.net/customer-api/teetimes-day"
                 f"?course={cid}&date={date.isoformat()}&players=1"
                 f"&holes=18&addons=off")
            try:
                r = s.get(u, timeout=25)
                print(f"   course={cid} -> {r.status_code} "
                      f"{r.text[:200].replace(chr(10), ' ')}")
            except Exception as e:  # noqa: BLE001
                print(f"   course={cid} -> EXC {e}")

    print("\n-- quick18 homestead: does the site still serve a tee sheet?")
    for url in ("https://homestead.quick18.com/",
                "https://homestead.quick18.com/teetimes/searchmatrix"):
        try:
            r = s.get(url, timeout=25, allow_redirects=True)
            print(f"   {url} -> {r.status_code} final={r.url} len={len(r.text)}")
        except Exception as e:  # noqa: BLE001
            print(f"   {url} -> EXC {e}")

    print("\n-- teeitup rollingstone-ranch: does the alias resolve?")
    ad = ADAPTERS["teeitup"]()
    for alias in ("rollingstone-ranch", "rollingstone-ranch-golf-club",
                  "rolling-stone-ranch"):
        try:
            fac = ad.discover_facilities(alias)
            print(f"   alias {alias!r}: {len(fac)} facilities "
                  f"{[(f.get('id'), f.get('name')) for f in fac][:6]}")
        except Exception as e:  # noqa: BLE001
            print(f"   alias {alias!r}: EXC {type(e).__name__}: "
                  f"{str(e)[:160]}")


def main() -> None:
    import zoneinfo
    today = dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()
    print("probe date (Denver today):", today)
    probe_spa_routes()
    probe_config_sweep(today)
    run_slugs("B. THE SIX MIS-SLUGGED CO COURSES, UNDER THEIR REAL SLUGS",
              RESLUG, today)
    run_slugs("C1. THE FIVE GENUINELY-BROKEN CO COURSES (re-run)",
              BROKEN, today)
    probe_broken_detail(today)


if __name__ == "__main__":
    main()
