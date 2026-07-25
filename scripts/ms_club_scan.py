"""Scan the MemberSports club-id space and map every club to its real courses.

Round 2 proved:
  * golfCourseId=0 + golfClubGroupId=0 returns exactly the courses that belong
    to `golfClubId`, and configurationTypeId selects WHICH tee sheet
    (Kennedy: cfg 0 = Par 3, cfg 1 = Babe Lind/Creek, cfg 2 = West 9 only).
  * golfClubGroupId=1 ignores golfClubId entirely and returns the whole Denver
    municipal group — which is why every club looked identical in the sweep.
  * There is NO readable club-listing endpoint (401/404 on all shapes) and the
    SPA bundle carries no literal route strings.

So the only way to find the true club id for Meadows, Lincoln Park, Boomerang
and Highland Hills is to walk the id space and ask each club what it has. Each
probe is one POST for a single date, which is exactly what a browser does when
you open that club's booking page.

Output: club id -> {golfCourseId: name} across a small configurationTypeId
sweep, printed only for clubs that answer with something.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

MS_API = "https://api.membersports.com/api/v1"
MS_KEY = os.environ.get("MEMBERSPORTS_API_KEY",
                        "A9814038-9E19-4683-B171-5A06B39147FC")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-api-key": MS_KEY,
    "Origin": "https://app.membersports.com",
    "Referer": "https://app.membersports.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}

LO = int(os.environ.get("MS_SCAN_LO", "3600"))
HI = int(os.environ.get("MS_SCAN_HI", "3900"))
CFGS = [int(x) for x in os.environ.get("MS_SCAN_CFGS", "0,1,2,3").split(",")]


def probe(club: int, date: str) -> tuple[int, dict]:
    """Return (club, {courseId: {name, cfgs:[...], rows:int}})."""
    found: dict = {}
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for cfg in CFGS:
        body = {"configurationTypeId": cfg, "date": date,
                "golfClubGroupId": 0, "golfClubId": club,
                "golfCourseId": 0, "groupSheetTypeId": 0}
        try:
            r = sess.post(f"{MS_API}/golfclubs/onlineBookingTeeTimes",
                          json=body, timeout=20)
        except Exception:  # noqa: BLE001 — a dead id is the common case
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, list) or not data:
            continue
        for row in data:
            for it in row.get("items", []):
                cid = it.get("golfCourseId")
                if cid is None:
                    continue
                e = found.setdefault(cid, {"name": it.get("name") or "",
                                           "cfgs": [], "slots": 0})
                if cfg not in e["cfgs"]:
                    e["cfgs"].append(cfg)
                e["slots"] += 1
                # A course id can carry different sheet names per cfg
                # (20573 is "Babe Lind / Creek" at cfg 1, "West 9 only" at 2).
                nm = it.get("name") or ""
                if nm and nm not in e["name"]:
                    e["name"] = f"{e['name']} | {nm}" if e["name"] else nm
    return club, found


def main() -> None:
    import zoneinfo
    date = (dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()
            + dt.timedelta(days=1)).isoformat()
    print(f"MemberSports club scan: ids {LO}..{HI}, cfgs {CFGS}, date {date}")
    print("(one POST per club per cfg; only clubs that return inventory are "
          "printed)\n")

    hits = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(probe, c, date) for c in range(LO, HI + 1)]
        for f in futures:
            club, found = f.result()
            if not found:
                continue
            hits += 1
            print(f"club {club}:")
            for cid, e in sorted(found.items()):
                print(f"    {cid:>6}  cfgs={e['cfgs']}  slots={e['slots']:>4}  "
                      f"{e['name']}")
            sys.stdout.flush()

    print(f"\n{hits} clubs with inventory in {LO}..{HI}")


if __name__ == "__main__":
    main()
