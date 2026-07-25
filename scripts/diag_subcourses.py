"""Diagnose sub-course identity and the known capture misses.

Runs in GitHub Actions (the dev sandbox can't reach booking hosts). Three parts:

  A. MEMBERSPORTS CLUB TOPOLOGY. The registry maps each course to one
     (golfClubId, golfCourseId) pair — but three pairs of physically distinct
     courses currently share an identical pair:
         Boomerang / Highland Hills   3724/4793
         Foothills  / Meadows         3697/4758
         Lincoln Park / Tiara Rado    3821/4918
     Whatever those ids point at, two different courses are being served the
     same tee sheet, so at least one of each pair is wrong. Kennedy is a
     related case: it asks for a single golfCourseId, so its other sheets can
     never appear and `multi` never trips. This probes for an endpoint that
     enumerates a club's courses, so the real ids can be written back.

  B. TEEITUP HYLAND HILLS. Confirm the multi-course path works: list the
     facilities the alias exposes and the courseIds an actual day returns.

  C. THE 14 CO MISSES. Run each through its production adapter and record the
     exact exception or the empty result, so the fixes can be targeted rather
     than guessed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

sys.path.insert(0, os.getcwd())

from scraper.aggregate import ADAPTERS  # noqa: E402

MS_API = "https://api.membersports.com/api/v1"
MS_KEY = os.environ.get("MEMBERSPORTS_API_KEY",
                        "A9814038-9E19-4683-B171-5A06B39147FC")
MS_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-api-key": MS_KEY,
    "Origin": "https://app.membersports.com",
    "Referer": "https://app.membersports.com/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
}

CLUBS = {
    3629: "Kennedy (Aurora) — expects Babe Lind / Creek / Park nines + Par 3",
    3724: "Boomerang + Highland Hills (Greeley) — SHARE ids today",
    3697: "Foothills + Meadows (Denver/Littleton) — SHARE ids today",
    3821: "Lincoln Park + Tiara Rado (Grand Junction) — SHARE ids today",
    3660: "City Park (Denver) — control, single course",
}

MISSES = [
    "emerald-greens-golf-course", "lake-arbor-golf-course",
    "university-of-denver-golf-club-at-highlands-ranch",
    "black-bear-golf-club", "desert-hawk-golf-course",
    "pelican-lakes-golf-country-club", "tamarack-golf-course",
    "walking-stick-golf-course", "homestead-golf-course",
    "granby-ranch-golf-course", "rollingstone-ranch-golf-club",
    "coyote-creek-golf-course", "hollydot-golf-course",
    "petteys-park-golf-course",
]


def jdump(label, obj, limit=1500):
    s = json.dumps(obj, default=str)
    print(f"   {label}: {s[:limit]}{' …TRUNC' if len(s) > limit else ''}")


def probe_membersports_topology(date: dt.date) -> None:
    print("=" * 72)
    print("A. MEMBERSPORTS CLUB TOPOLOGY")
    print("=" * 72)
    for club_id, why in CLUBS.items():
        print(f"\n--- club {club_id}: {why}")

        # Candidate read endpoints. MemberSports' app has to learn a club's
        # course list somehow; try the obvious shapes and report which answers.
        for path in (f"/golfclubs/{club_id}",
                     f"/golfclubs/{club_id}/golfcourses",
                     f"/golfclubs/{club_id}/onlineBookingGolfCourses",
                     f"/golfclubs/{club_id}/onlineBookingConfiguration"):
            try:
                r = requests.get(MS_API + path, headers=MS_HEADERS, timeout=20)
                body = r.text[:400].replace("\n", " ")
                print(f"   GET {path} -> {r.status_code} {body}")
            except Exception as e:  # noqa: BLE001
                print(f"   GET {path} -> EXC {e}")

        # The tee-sheet call itself: does golfCourseId=0 mean "all courses"?
        for course_id in (0, -1):
            body = {"configurationTypeId": 0, "date": date.isoformat(),
                    "golfClubGroupId": 0, "golfClubId": club_id,
                    "golfCourseId": course_id, "groupSheetTypeId": 0}
            try:
                r = requests.post(f"{MS_API}/golfclubs/onlineBookingTeeTimes",
                                  json=body, headers=MS_HEADERS, timeout=25)
                if r.status_code != 200:
                    print(f"   POST teeTimes golfCourseId={course_id} -> "
                          f"{r.status_code} {r.text[:200]}")
                    continue
                data = r.json()
                if not isinstance(data, list):
                    print(f"   POST teeTimes golfCourseId={course_id} -> "
                          f"non-list {type(data).__name__}")
                    continue
                seen = {}
                for row in data:
                    for it in row.get("items", []):
                        cid = it.get("golfCourseId")
                        if cid is not None:
                            seen.setdefault(cid, it.get("name") or "")
                print(f"   POST teeTimes golfCourseId={course_id} -> "
                      f"{len(data)} rows, courseIds {seen}")
                if data:
                    jdump("first row", data[0], 600)
            except Exception as e:  # noqa: BLE001
                print(f"   POST teeTimes golfCourseId={course_id} -> EXC {e}")


def probe_hyland(date: dt.date) -> None:
    print()
    print("=" * 72)
    print("B. TEEITUP — HYLAND HILLS SUB-COURSES")
    print("=" * 72)
    ad = ADAPTERS["teeitup"]()
    alias = "hyland-hills-park-recreation-district"
    try:
        fac = ad.discover_facilities(alias)
        print(f"   facilities exposed by alias: {len(fac)}")
        for f in fac:
            print("     ", {k: f.get(k) for k in ("id", "courseId", "name",
                                                  "timezone", "alias")})
    except Exception as e:  # noqa: BLE001
        print("   discover_facilities EXC:", e)

    course = {"slug": "the-greg-mastriona-golf-courses-at-hyland-hills",
              "name": "The Greg Mastriona Golf Courses At Hyland Hills",
              "city": "Westminster", "state": "CO", "platform": "teeitup",
              "ids": {"alias": alias},
              "booking_url": f"https://{alias}.book.teeitup.com/"}
    for d in (date, date + dt.timedelta(days=1)):
        try:
            tts = ad.fetch(course, d)
            labels = {}
            for t in tts:
                lab = getattr(t, "course_label", "") or "(blank)"
                labels[lab] = labels.get(lab, 0) + 1
            print(f"   {d}: {len(tts)} slots | labels {labels}")
        except Exception as e:  # noqa: BLE001
            print(f"   {d}: EXC {type(e).__name__}: {e}")


def probe_misses(date: dt.date) -> None:
    print()
    print("=" * 72)
    print("C. THE 14 COLORADO CAPTURE MISSES")
    print("=" * 72)
    reg = {c["slug"]: c for c in json.load(open("registry.json"))["courses"]}
    for slug in MISSES:
        c = reg.get(slug)
        if not c:
            print(f"\n--- {slug}: NOT IN REGISTRY (slug drifted?)")
            near = [s for s in reg if slug.split("-")[0] in s]
            print("    similar slugs:", near[:6])
            continue
        plat = c.get("platform")
        print(f"\n--- {slug} [{plat}] ids={c.get('ids')}")
        print(f"    url: {c.get('booking_url')}")
        ad_cls = ADAPTERS.get(plat)
        if not ad_cls:
            print("    no adapter registered for this platform")
            continue
        ad = ad_cls()
        for d in (date, date + dt.timedelta(days=1)):
            try:
                tts = ad.fetch(c, d)
                print(f"    {d}: {len(tts)} slots"
                      + (f" | first {tts[0].teetime}" if tts else " (EMPTY)"))
            except Exception as e:  # noqa: BLE001
                msg = str(e).replace("\n", " ")[:220]
                print(f"    {d}: EXC {type(e).__name__}: {msg}")


def main() -> None:
    import zoneinfo
    today = dt.datetime.now(zoneinfo.ZoneInfo("America/Denver")).date()
    print("probe date (Denver today):", today)
    probe_membersports_topology(today)
    probe_hyland(today)
    probe_misses(today)


if __name__ == "__main__":
    main()
