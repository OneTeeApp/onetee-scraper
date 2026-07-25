"""Verify two outstanding MemberSports leads before either is registered.

Two rows are still unresolved:

  Homestead Golf Course (Lakewood) — registered as quick18/homestead, which
    has returned 0 slots on every scrape (diag4: flat 18-19KB pages, no time
    cells across 7 days). The browser probe of golflakewood.com found the city
    links TWO MemberSports sheets (probe-results/browser_native.txt):
        app.membersports.com/tee-times/3703/20589/0/7/0   <- Fox Hollow, known
        app.membersports.com/tee-times/3807/4902/0/7/0    <- by elimination,
                                                             Homestead
  Meadows Golf Club (Littleton) — currently needs_ids. foothillsgolf.org links
        app.membersports.com/online-store/3811/4906/225
    and club 3697 (Foothills) owns only sheets named Foothills, so 3811 is the
    Meadows lead.

"By elimination" is not good enough to publish on. The failure mode we have
already hit twice — Boomerang serving Highland Hills' sheet, Lincoln Park
serving Tiara Rado's — is a club id that belongs to a NEIGHBOUR, and the thing
that gives it away is the item names on the sheet. So this sweeps
configurationTypeId 0..8 for each candidate club and prints, per cfg, every
(golfCourseId, item name) it returns with slot counts. A club whose items say
"Homestead" is Homestead; a club whose items say "Fox Hollow" is not.

Fox Hollow (3703) and Foothills (3697) are swept as controls, so we can see
what a known-correct club looks like in the same output and confirm the
candidate is not just echoing them.

Report only. Nothing here edits the CSV or the registry.
Public API, no credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import sys
import traceback

sys.path.insert(0, ".")

from scraper.adapters.experimental import MemberSportsAdapter  # noqa: E402
from scraper.aggregate import ADAPTERS  # noqa: E402

# (label, club_id, secondary_id from the booking URL, why)
CLUBS = [
    ("Homestead CANDIDATE", 3807, 4902,
     "golflakewood.com links tee-times/3807/4902/0/7/0"),
    ("Meadows CANDIDATE", 3811, 4906,
     "foothillsgolf.org links online-store/3811/4906/225"),
    ("Fox Hollow CONTROL", 3703, 20589,
     "already registered and publishing"),
    ("Foothills CONTROL", 3697, 4758,
     "already registered; shares the parks district with Meadows"),
]

CFGS = list(range(0, 9))
DATES = [dt.date.today() + dt.timedelta(days=d) for d in (2, 5)]


def sweep(label: str, club_id: int, secondary: int, why: str) -> dict:
    """Print every (courseId, item name) this club returns, per cfg."""
    print(f"\n--- {label}: club {club_id}  (url course id {secondary})")
    print(f"    {why}")
    ad = MemberSportsAdapter()
    found: dict[tuple, int] = {}
    for date in DATES:
        for cfg in CFGS:
            try:
                data = ad._sheet(club_id, cfg, date)
            except Exception as exc:  # noqa: BLE001
                print(f"    {date} cfg {cfg}: ERROR {type(exc).__name__}: "
                      f"{str(exc)[:110]}")
                continue
            per: dict[tuple, int] = {}
            for row in data:
                if row.get("teeTime") is None:
                    continue
                for it in row.get("items", []) or []:
                    key = (it.get("golfCourseId"), (it.get("name") or "").strip())
                    per[key] = per.get(key, 0) + 1
                    found[key] = found.get(key, 0) + 1
            if not per:
                print(f"    {date} cfg {cfg}: 0 rows")
                continue
            print(f"    {date} cfg {cfg}: {len(data)} rows")
            for (cid, name), n in sorted(per.items(), key=lambda kv: -kv[1]):
                print(f"        courseId={cid!s:<8} {n:>4} items  {name!r}")
            sys.stdout.flush()

    print(f"    SUMMARY club {club_id}: "
          f"{len(found)} distinct (courseId, name) pairs")
    for (cid, name), n in sorted(found.items(), key=lambda kv: -kv[1]):
        print(f"        courseId={cid!s:<8} {n:>4} total  {name!r}")
    return found


def adapter_check(slug: str, name: str, club_id: int, cfgs: list[int]) -> None:
    """What the real adapter would publish for this row, end to end."""
    print(f"\n--- adapter dry-run: {name}  club {club_id} cfgs {cfgs}")
    ad = ADAPTERS["membersports"]()
    course = {"slug": slug, "name": name, "city": "", "state": "CO",
              "platform": "membersports", "venue_id": slug,
              "source_role": "primary", "status": "ready", "booking_url": "",
              "lat": None, "lon": None,
              "ids": {"club_id": str(club_id), "config_ids": cfgs}}
    for date in DATES:
        try:
            slots = ad.fetch(course, date)
        except Exception as exc:  # noqa: BLE001
            print(f"    {date}: ERROR {type(exc).__name__}: {str(exc)[:140]}")
            continue
        labels = sorted({(t.course_label or "").strip() for t in slots} - {""})
        if slots:
            times = sorted(str(t.teetime) for t in slots)
            print(f"    {date}: {len(slots)} slots  "
                  f"{times[0][11:16]}-{times[-1][11:16]}  "
                  f"labels={labels or ['(none)']}")
        else:
            print(f"    {date}: 0 slots")
        sys.stdout.flush()


def main() -> None:
    print("verify_membersports: are clubs 3807 and 3811 really Homestead and "
          "Meadows?")
    print(f"dates: {', '.join(d.isoformat() for d in DATES)}  "
          f"cfgs: {CFGS[0]}-{CFGS[-1]}")
    print("Report only. Nothing here edits the CSV or the registry.")

    found: dict[int, dict] = {}
    for label, club_id, secondary, why in CLUBS:
        try:
            found[club_id] = sweep(label, club_id, secondary, why)
        except Exception:  # noqa: BLE001
            print("    HARNESS ERROR:")
            traceback.print_exc(limit=3)
        sys.stdout.flush()

    print("\n" + "=" * 70)
    print("Do the candidates overlap the controls? (an overlap means the "
          "candidate club id is the neighbour's, not its own)")
    print("=" * 70)
    for cand in (3807, 3811):
        for ctrl in (3703, 3697):
            a, b = found.get(cand, {}), found.get(ctrl, {})
            shared = sorted(set(a) & set(b))
            print(f"  club {cand} vs {ctrl}: "
                  f"{len(shared)} shared (courseId, name) pairs"
                  + (f" -> {shared[:4]}" if shared else ""))

    print("\n" + "=" * 70)
    print("Adapter dry-run with every cfg that returned anything")
    print("=" * 70)
    for slug, name, club_id in (("homestead-golf-course", "Homestead Golf Course", 3807),
                                ("meadows-golf-club", "Meadows Golf Club", 3811)):
        try:
            adapter_check(slug, name, club_id, CFGS)
        except Exception:  # noqa: BLE001
            print("    HARNESS ERROR:")
            traceback.print_exc(limit=3)

    print("\ndone")


if __name__ == "__main__":
    main()
