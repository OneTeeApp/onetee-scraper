"""Which silent Florida TeeItUp courses are broken, and which are merely empty?

Florida is our largest TeeItUp state by far — 214 of its ready booking sources
are teeitup, more than triple Arizona's. That is the same shape Arizona had
before scripts/probe_teeitup_az.py split its eighteen silents into distinct
repairs, so this asks the same four questions across Florida's silent set.

WAIT BEFORE YOU SUSPECT AN ALIAS. This probe was written to answer whether the
sixteen teeitup rows commit e7964e1 added were dark because their aliases were
wrong. They were not. Measured three hours after landing: 0 of 16 serving.
Measured five hours later: 13 of 16 serving, with the state's silent teeitup
count falling 87 -> 60 over the same window. The cause was the paced sweep's
latency, and a probe fired at the three-hour mark would have burned kenna
requests to rediscover that. On a freshly added row, give the sweep most of a
day before treating silence as evidence of anything.

What still justifies the probe is the residue: rows that stay dark well past
that window. Their booking URLs came from links published on each course's OWN
site, which feels authoritative and is not — the vanity booking host and the
kenna x-be-alias are two different namespaces. Golden Hills served a real 29KB
tenant page on a host kenna had never heard of, and Meadowcreek in Virginia
was the sixth instance. A wrong alias and an un-swept course look identical
from outside; only the facilities route separates them:

  facilities  does kenna know the alias at all (404 = wrong alias, the
              Golden Hills case — fix is one registry field)
  sheet       bare AND pinned across spread dates, so "empty without the pin,
              full with it" is a visible difference (the shared-tenant case:
              Florida has several, e.g. the Orange County National and Kings
              Ridge rows that already carry ?course= pins)
  fetch       the real production path, same as the hourly scan
  page        which backend and alias the booking host's bundle names

Targets are derived FROM THE REGISTRY crossed with the hourly inventory
sample, never hand-typed, so the probe re-aims itself at whatever is silent
today rather than at a list that was true once; pass --slugs to narrow.
Controls are Florida aliases confirmed serving, so a fleet-wide kenna throttle
during the run cannot read as dozens of dead courses — the lesson from the two
AZ probe runs that 429 poisoning invalidated.

Report only: public GETs, no D1 writes, no registry or CSV edits.

  python3 scripts/probe_teeitup_fl.py [--out probe-results/teeitup-fl.json]
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

# Controls must be courses that serve RELIABLY, not ones that served once.
# The first draft used blackstone-golf-course on the strength of a single
# WebFetch reading; the monitor's four samples across 2026-07-29 show it has
# never served at all, which would have made the probe self-invalidate on every
# run and report its own targets as unknown. Both of these served in all four
# samples with hundreds of slots each. Two shapes on purpose: a plain
# single-facility alias, and a facility-pinned tenant, so the shared-tenant
# mechanics have a known-good example in the report.
CONTROL_SLUGS = [
    "colony-west-golf-club-glades-course",   # plain alias, 487 slots
    "kissimmee-bay-country-club",            # facility_id 9093 pinned, 481 slots
]

# Added by e7964e1 from links on each course's own site. Kept as a label, not
# a target list: measured 2026-07-29, 13 of these 16 came live within five
# hours of landing, which answered the question this probe was written for —
# the cause was sweep latency, not aliases. Marking them in the report keeps
# "brand new" and "broken alias" apart when reading a later run.
NEWLY_ADDED = {
    "boca-dunes-golf-country-club",
    "cross-creek-country-club",
    "esplanade-at-azario-golf-club",
    "indianwood-golf-country-club",
    "lake-diamond-golf-country-club",
    "little-sandy-at-omni-amelia-island-resort",
    "miami-springs-golf-country-club",
    "shalimar-pointe-golf-club",
    "stonecrest-golf-club",
    "the-country-club-at-lake-city",
    "the-landings-golf-club",
    "the-links-of-spruce-creek-south",
    "the-oaks-golf-club",
    "the-preserve-golf-club-at-spruce-creek-preserve",
    "waterlefe-golf-river-club",
    "zellwood-station-country-club",
}

# The hourly monitor's newest sample. Using it to choose targets is what keeps
# this probe useful on the second run and not just the first: silence is a
# moving quantity, and a hand-pasted list of dark courses libels every course
# that started serving after it was pasted. This file is refreshed hourly in
# the repo, and the probe re-measures every target anyway, so a stale entry
# costs one wasted probe rather than a wrong verdict.
INVENTORY = "probe-results/inventory-history.jsonl"

DATE_OFFSETS = (1, 3, 7)


def serving_now(path: str, state: str) -> set[str] | None:
    """Venue slugs the newest inventory sample shows serving, or None."""
    try:
        with open(path) as fh:
            last = [ln for ln in fh if ln.strip()][-1]
        return set(json.loads(last)["states"][state]["courses"])
    except (OSError, ValueError, KeyError, IndexError):
        return None


def build_targets(reg: dict[str, dict], slugs: list[str] | None,
                  every: bool) -> list[dict]:
    """Targets plus serving controls.

    DEFAULT is every FL teeitup row the registry calls ready that the newest
    hourly sample shows serving nothing — the courses about which there is an
    open question, and no others. `--all` widens to all ready FL teeitup rows
    including the serving ones, and that is deliberately not the default: the
    AZ probe ran eighteen targets, and an order of magnitude more volume
    against one shared host is how kenna starts 429ing. A 429 storm turns
    every target empty, which reads exactly like a dead fleet — two earlier AZ
    runs were discarded for precisely that.

    If the inventory file cannot be read, this falls back to the rows e7964e1
    added rather than silently probing all 214, so a missing artifact costs
    coverage visibly instead of triggering the throttle it exists to avoid.
    """
    ready = [c["slug"] for c in reg.values()
             if c.get("state") == "FL" and c.get("platform") == "teeitup"
             and c.get("status") == "ready" and c["slug"] not in CONTROL_SLUGS]
    if slugs:
        chosen = [s for s in slugs if s not in CONTROL_SLUGS]
    elif every:
        chosen = sorted(ready)
    else:
        live = serving_now(INVENTORY, "FL")
        if live is None:
            print(f"NOTE: {INVENTORY} unreadable — falling back to the rows "
                  f"e7964e1 added. Ready FL teeitup rows outside that set are "
                  f"NOT covered by this run.")
            chosen = sorted(s for s in NEWLY_ADDED if s not in CONTROL_SLUGS)
        else:
            chosen = sorted(s for s in ready if s not in live)
    targets = []
    for role, group in (("target", chosen), ("control", CONTROL_SLUGS)):
        for slug in group:
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
                "new": slug in NEWLY_ADDED,
            })
    return targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="probe-results/teeitup-fl.json")
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--slugs", default="",
                    help="comma-separated subset to probe instead of the default")
    ap.add_argument("--all", action="store_true",
                    help="probe every ready FL teeitup row (214) including the "
                         "ones already serving, not just the silent ones. High "
                         "request volume against one shared host — do not run "
                         "this while a far sweep is active.")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the target count (0 = no cap). A cap is a "
                         "SILENT loss of coverage, so it is logged.")
    a = ap.parse_args()

    today = dt.date.today()
    dates = [today + dt.timedelta(days=d) for d in DATE_OFFSETS]
    ad = TeeItUpAdapter()
    reg = {c["slug"]: c for c in load_registry(a.registry)}
    slugs = [s.strip() for s in a.slugs.split(",") if s.strip()]
    targets = build_targets(reg, slugs or None, a.all)
    if a.limit and len(targets) > a.limit:
        dropped = len(targets) - a.limit
        print(f"NOTE: --limit {a.limit} drops {dropped} targets from this run; "
              f"they are NOT covered by this report.")
        targets = targets[:a.limit]

    print("probe_teeitup_fl: which silent FL aliases are broken vs merely empty")
    print(f"targets: {sum(1 for t in targets if t['role'] == 'target')} "
          f"({sum(1 for t in targets if t.get('new'))} added by e7964e1), "
          f"controls: {sum(1 for t in targets if t['role'] == 'control')}")
    print(f"dates: {', '.join(d.isoformat() for d in dates)}")
    print("Report only. No writes to D1, the registry or the CSV.\n")
    sys.stdout.flush()

    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "dates": [d.isoformat() for d in dates],
                 "api_base": API_BASE,
                 "targets": []}

    for t in targets:
        rec: dict = {"slug": t["slug"], "alias": t["alias"], "role": t["role"],
                     "new": t.get("new", False)}
        print("=" * 72)
        print(f"{t['slug']}  ({t['role']}{', NEW' if t.get('new') else ''})"
              f"   alias={t['alias']}")
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
        tag = "NEW " if rec.get("new") else "    "
        print(f"  {tag}{rec['slug']:<48} {rec['role']:<8} {rec['verdict']}")
    print(f"\n  by class: {counts}")

    controls = [r for r in out["targets"] if r["role"] == "control"]
    if controls and not any(r["verdict"].startswith("serving") for r in controls):
        # Two earlier AZ runs were invalidated exactly this way: kenna 429s
        # during a concurrent far sweep turned every target empty, which is
        # indistinguishable from a genuinely dark fleet unless the controls
        # are checked. Say so loudly rather than letting the file be read as
        # eighty-seven dead courses.
        print("\n  *** CONTROLS DID NOT SERVE — treat every empty verdict in "
              "this run as UNKNOWN, not as a dead course. Re-run when no far "
              "sweep is active. ***")
        out["controls_failed"] = True

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
