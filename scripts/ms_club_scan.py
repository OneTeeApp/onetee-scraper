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

Round 3 (this version). The first scan found only 2 of 301 clubs, which
contradicts the round-2 diag where clubs 3697/3821/3660 all returned rows on
the same dates. That gap was throttling, not absence: 8 workers x 4 cfgs fired
~1200 POSTs and every non-200 was silently skipped. So:

  * PASS 1 sweeps the whole id range at cfg 0 ONLY, 3 workers, with retry and
    backoff on 429/5xx and on transport errors.
  * PASS 2 re-probes just the clubs that answered with inventory across the
    remaining cfgs, which is a few dozen requests rather than a few thousand.
  * Every HTTP status is counted and printed. An id that answers 200-with-[]
    is genuinely empty; an id that only ever errored is UNKNOWN and is listed
    separately, so a throttled run can never again read as a clean negative.
  * Two dates are tried (tomorrow, then +3 days) before calling a club empty,
    because a course with no sheet open tomorrow still answers for later.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from collections import Counter
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

LO = int(os.environ.get("MS_SCAN_LO", "3550"))
HI = int(os.environ.get("MS_SCAN_HI", "4000"))
CFGS = [int(x) for x in os.environ.get("MS_SCAN_CFGS", "0,1,2,3,4,5,7").split(",")]
WORKERS = int(os.environ.get("MS_SCAN_WORKERS", "3"))
TRIES = 4

STATUS = Counter()


def post(sess: requests.Session, club: int, cfg: int, date: str):
    """POST one tee sheet. Returns (ok, data_or_None).

    ok=False means we never got a clean answer for this (club, cfg, date) and
    the caller must NOT treat it as 'no inventory'.
    """
    body = {"configurationTypeId": cfg, "date": date,
            "golfClubGroupId": 0, "golfClubId": club,
            "golfCourseId": 0, "groupSheetTypeId": 0}
    for attempt in range(TRIES):
        try:
            r = sess.post(f"{MS_API}/golfclubs/onlineBookingTeeTimes",
                          json=body, timeout=25)
        except Exception as exc:  # noqa: BLE001
            STATUS[f"EXC {type(exc).__name__}"] += 1
            time.sleep(1.5 * (attempt + 1))
            continue
        STATUS[r.status_code] += 1
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                STATUS["bad-json"] += 1
                return False, None
            return True, (data if isinstance(data, list) else [])
        if r.status_code in (400, 401, 403, 404):
            # A definite answer: this club/cfg is not bookable. Not a retry.
            return True, []
        time.sleep(1.5 * (attempt + 1))          # 429 / 5xx
    return False, None


def collect(found: dict, data: list, cfg: int) -> None:
    for row in data or []:
        for it in row.get("items", []):
            cid = it.get("golfCourseId")
            if cid is None:
                continue
            e = found.setdefault(cid, {"name": "", "cfgs": [], "slots": 0})
            if cfg not in e["cfgs"]:
                e["cfgs"].append(cfg)
            e["slots"] += 1
            # A course id can carry different sheet names per cfg
            # (20573 is "Babe Lind / Creek" at cfg 1, "West 9 only" at 2).
            nm = (it.get("name") or "").strip()
            if nm and nm not in e["name"]:
                e["name"] = f"{e['name']} | {nm}" if e["name"] else nm


def pass1(club: int, dates: list[str]) -> tuple[int, dict, bool]:
    """cfg 0 across the candidate dates. Returns (club, found, answered)."""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    found: dict = {}
    answered = False
    for date in dates:
        ok, data = post(sess, club, 0, date)
        answered = answered or ok
        if ok and data:
            collect(found, data, 0)
            break          # one good date is enough to identify the club
    return club, found, answered


def pass2(club: int, dates: list[str]) -> tuple[int, dict]:
    """Remaining cfgs for a club already known to exist."""
    sess = requests.Session()
    sess.headers.update(HEADERS)
    found: dict = {}
    for cfg in CFGS:
        for date in dates:
            ok, data = post(sess, club, cfg, date)
            if ok and data:
                collect(found, data, cfg)
                break
    return club, found


def main() -> None:
    import zoneinfo
    today = dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()
    dates = [(today + dt.timedelta(days=d)).isoformat() for d in (1, 3)]
    print(f"MemberSports club scan (round 3): ids {LO}..{HI}, "
          f"cfgs {CFGS}, dates {dates}, workers {WORKERS}")
    print("PASS 1: cfg 0 over the whole range, with retry/backoff.\n")

    live: dict[int, dict] = {}
    unknown: list[int] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for club, found, answered in ex.map(
                lambda c: pass1(c, dates), range(LO, HI + 1)):
            if found:
                live[club] = found
                print(f"  club {club}: " + ", ".join(
                    f"{cid}={e['name']!r}" for cid, e in sorted(found.items())))
                sys.stdout.flush()
            elif not answered:
                unknown.append(club)

    print(f"\nPASS 1 done: {len(live)} clubs with inventory, "
          f"{len(unknown)} never answered cleanly.")
    if unknown:
        print("  UNKNOWN (all attempts failed — NOT proven empty):")
        print("   ", ", ".join(str(c) for c in unknown))

    print(f"\nPASS 2: cfgs {CFGS} for the {len(live)} live clubs.\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for club, found in ex.map(lambda c: pass2(c, dates), sorted(live)):
            merged = live[club]
            for cid, e in found.items():
                m = merged.setdefault(cid, {"name": "", "cfgs": [], "slots": 0})
                for cfg in e["cfgs"]:
                    if cfg not in m["cfgs"]:
                        m["cfgs"].append(cfg)
                m["slots"] += e["slots"]
                if e["name"] and e["name"] not in m["name"]:
                    m["name"] = (f"{m['name']} | {e['name']}"
                                 if m["name"] else e["name"])
            print(f"club {club}:")
            for cid, e in sorted(merged.items()):
                print(f"    {cid:>6}  cfgs={sorted(e['cfgs'])}  "
                      f"slots={e['slots']:>5}  {e['name']}")
            sys.stdout.flush()

    print("\nHTTP status histogram:")
    for k, v in sorted(STATUS.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
