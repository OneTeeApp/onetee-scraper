"""Why do 18 Arizona TeeItUp courses return nothing while ~50 work?

Arizona's coverage gap is dominated by one bucket: 18 TeeItUp rows the
registry calls ready that serve zero tee times. Colorado's version of this
question (scripts/probe_teeitup_co.py) split three silents into three
different repairs — a booking window misread as silence (Trinidad), a course
that physically moved platforms (Granby), and a real anonymous-empty
(Rollingstone). Eighteen courses is too many to hand-diagnose one at a time,
so this runs the same four-question probe across all of them at once:

  facilities  does kenna know the alias at all (404 = wrong alias, the
              Golden Hills case — fix is one registry field)
  sheet       bare AND pinned across spread dates, so "empty without the
              pin, full with it" is a visible difference (the Aguila 9 case
              would show here: it shares the city-of-phoenix tenant and MUST
              be pinned to facility 4322)
  fetch       the real production path, same as the hourly scan
  page        which backend and alias the booking host's bundle names

Controls are serving AZ aliases: longbow (plain single-facility) and
cave-creek (the SAME shared city-of-phoenix tenant as silent Aguila 9, pinned
to 288 and serving) — so a fleet-wide kenna throttle during the run cannot
read as eighteen dead courses, and the shared-tenant mechanics have a known-
good example right in the report.

Targets are built FROM THE REGISTRY, not hand-typed: alias and any pinned
facility_id come from each row's ids, so the probe tests what production
actually uses. Report only — no writes to D1, the registry or the CSV.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper.adapters.teeitup import API_BASE, TeeItUpAdapter  # noqa: E402
from scraper.aggregate import fetch_course, load_registry  # noqa: E402

# Reuse the CO probe's machinery verbatim — same questions, same discipline
# that a missing answer is never recorded as a known one.
_co_path = pathlib.Path(__file__).parent / "probe_teeitup_co.py"
_spec = importlib.util.spec_from_file_location("probe_teeitup_co", _co_path)
co = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(co)

SILENT_SLUGS = [
    "aguila-9-golf-course",
    "ahwatukee-country-club",
    "antelope-hills-golf-course",
    "arizona-national-golf-club",
    "augusta-ranch-golf-club",
    "canoa-ranch-golf-club",
    "desert-springs-golf-course",
    "falcon-golf-club",
    "fountain-of-the-sun-country-club",
    "francisco-grande-hotel-golf-resort",
    "great-eagle-golf-club",
    "junior-national-golf-club",
    "mountain-brook-golf-club",
    "omni-tucson-national",
    "san-tan-highlands-golf-club",
    "sierra-vista-golf-center-at-pueblo-del-sol",
    "ventana-canyon-golf-racquet-club",
    "villa-de-paz-golf-club",
]
CONTROL_SLUGS = ["longbow-golf-club", "cave-creek-golf-course"]

DATE_OFFSETS = (1, 3, 7)


def build_targets(reg: dict[str, dict]) -> list[dict]:
    targets = []
    for role, slugs in (("target", SILENT_SLUGS), ("control", CONTROL_SLUGS)):
        for slug in slugs:
            c = reg.get(slug)
            if c is None:
                targets.append({"slug": slug, "role": role, "alias": None,
                                "pins": [], "page": "",
                                "note": "SLUG NOT IN REGISTRY"})
                continue
            ids = c.get("ids") or {}
            targets.append({
                "slug": slug, "role": role,
                "alias": ids.get("alias"),
                "pins": [ids["facility_id"]] if ids.get("facility_id") else [],
                "page": c.get("booking_url") or "",
            })
    return targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="probe-results/teeitup-az.json")
    ap.add_argument("--registry", default="registry.json")
    a = ap.parse_args()

    today = dt.date.today()
    dates = [today + dt.timedelta(days=d) for d in DATE_OFFSETS]
    ad = TeeItUpAdapter()
    reg = {c["slug"]: c for c in load_registry(a.registry)}
    targets = build_targets(reg)

    print("probe_teeitup_az: why 18 AZ aliases return nothing while ~50 work")
    print(f"dates: {', '.join(d.isoformat() for d in dates)}")
    print("Report only. No writes to D1, the registry or the CSV.\n")

    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "dates": [d.isoformat() for d in dates],
                 "api_base": API_BASE,
                 "targets": []}

    for t in targets:
        rec: dict = {"slug": t["slug"], "alias": t["alias"], "role": t["role"]}
        print("=" * 72)
        print(f"{t['slug']}  ({t['role']})   alias={t['alias']}")
        print("=" * 72)
        if t["alias"] is None:
            rec["verdict"] = t.get("note", "no alias in registry")
            print(f"  SKIP: {rec['verdict']}\n")
            out["targets"].append(rec)
            continue

        rec["facilities_route"] = co.probe_route(
            ad, f"{API_BASE}/alias/{t['alias']}/facilities", t["alias"])
        fr = rec["facilities_route"]
        if fr.get("ok"):
            print(f"  facilities: {fr['count']} listed")
            for f in fr["facilities"][:8]:
                print(f"      id={str(f['id']):<8} "
                      f"courseId={str(f['courseId'])[:26]:<26} "
                      f"tz={str(f['timeZone'] or ''):<20} {f['name']}")
        else:
            print(f"  facilities: FAILED {fr.get('http') or ''} "
                  f"{fr.get('error') or fr.get('body', '')[:120]}")
        sys.stdout.flush()

        discovered = [str(f["id"]) for f in fr.get("facilities", [])
                      if f.get("id") is not None]
        pins: list[str | None] = [None] + list(t["pins"]) + \
            [d for d in discovered if d not in t["pins"]]

        rec["sheets"] = {}
        for pin in pins[:4]:
            key = pin or "(bare)"
            rec["sheets"][key] = {}
            for d in dates:
                res = co.probe_sheet(ad, t["alias"], d, pin)
                rec["sheets"][key][d.isoformat()] = res
                print(f"  sheet pin={key:<10} {d.isoformat()}  "
                      f"{res['result']:<6} {res.get('slots', 0):>4} slots"
                      + (f"   {res['error']}" if res.get("error") else ""))
                sys.stdout.flush()

        course = reg.get(t["slug"])
        rec["fetch_course"] = {}
        for d in dates:
            res = fetch_course(course, d)
            rec["fetch_course"][d.isoformat()] = (
                {"result": "rows" if res.tee_times else "empty",
                 "slots": len(res.tee_times)} if res.ok
                else {"result": "error", "error": str(res.error)[:300]})
            r = rec["fetch_course"][d.isoformat()]
            print(f"  fetch_course        {d.isoformat()}  "
                  f"{r['result']:<6} {r.get('slots', 0):>4} slots"
                  + (f"   {r['error'][:120]}" if r.get("error") else ""))
            sys.stdout.flush()

        if t["page"]:
            rec["page"] = co.probe_page(ad, t["page"])
            p = rec["page"]
            print(f"  page {t['page']}")
            print(f"      HTTP {p.get('http')}  {p.get('bytes')}B  "
                  f"final={p.get('final_url')}")
            print(f"      api hosts named: {p.get('api_hosts')}")
            print(f"      aliases named:   {p.get('aliases_named')}")
        print()
        sys.stdout.flush()
        out["targets"].append(rec)

    print("=" * 72)
    print("VERDICTS")
    print("=" * 72)
    counts: dict[str, int] = {}
    for rec in out["targets"]:
        if "verdict" not in rec:
            rec["verdict"] = co.verdict_for(rec)
        v = rec["verdict"].split(":")[0]
        counts[v] = counts.get(v, 0) + 1
        print(f"  {rec['slug']:<44} {rec['role']:<8} {rec['verdict']}")
    print(f"\n  by class: {counts}")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
