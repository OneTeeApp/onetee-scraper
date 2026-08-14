"""Did the area search return the same inventory as the facility search?

Companion to .github/workflows/probe-golfnow-batching.yml. Reads the docs that
run left in probe-out/ and prints one table.

PAGE LOADS ARE THE PRIZE, PARITY IS THE GATE. Batching replaces 69 facility page
loads with 32 area page loads, which is a 54% cut and worth having. It is worth
nothing if an area search quietly returns fewer courses, and that failure would
be invisible in production: browser_golfnow ends every invocation in `|| true`,
and d1.sync scopes deactivation to the courses present in the scrape — so a
course the area path silently dropped keeps its stale rows until they age out,
looking healthy the whole time.

So the hard gate is COURSE PRESENCE, not slot count. A course that answered in
BOTH bracketing facility passes must answer in the area pass. Slot counts drift
between passes as inventory books and releases, and the two facility passes
bracket that drift so it can be told apart from a real loss.

The first thing printed is the shape diagnostic, because if the area response
carries no tee-time-shaped objects at all then the idea is dead and nothing
below it matters.
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict

OUTS = pathlib.Path("probe-out")


def load(tag: str) -> dict | None:
    f = OUTS / f"{tag}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"  ! unreadable {f.name}: {e}")
        return None


def per_course(doc: dict | None) -> tuple[dict[str, int], set[str]]:
    """slug -> slot count, plus the set of slugs that errored."""
    if not doc:
        return {}, set()
    counts: dict[str, int] = defaultdict(int)
    for t in doc.get("tee_times") or []:
        counts[t.get("course_slug")] += 1
    errs = {e.get("course") for e in doc.get("errors") or []}
    return dict(counts), errs


def main() -> int:
    plan_f = pathlib.Path("clusters.json")
    if not plan_f.is_file():
        print("no clusters.json — nothing to compare")
        return 0
    plan = json.loads(plan_f.read_text())
    clusters = plan["clusters"]
    members = [m for c in clusters for m in c["members"]]
    wanted = [m["slug"] for m in members]
    cluster_of = {m["slug"]: c["centre_name"] for c in clusters for m in c["members"]}

    area = load("gn_area2")
    fac1, fac3 = load("gn_fac1"), load("gn_fac3")
    if not area:
        print("no area output — the area pass produced nothing at all")
        return 0

    print("=" * 78)
    print("SHAPE — did the area response carry tee times, and where")
    print("=" * 78)
    any_paths = False
    for d in area.get("diagnostics") or []:
        paths = d.get("harvest_paths") or {}
        any_paths = any_paths or bool(paths)
        print(f"  {d['cluster'][:34]:<34} members={d['members']:<3} "
              f"pages={d.get('pages')} server_total={d.get('server_total')} "
              f"raw={d.get('raw_tee_time_objects_seen')} kept={d.get('kept_slots')} "
              f"facilities_answered={d.get('facilities_answered')}/{d['members']}")
        print(f"      predicate {d.get('predicate_scope')}")
        print(f"      tee times found at: {paths or 'NOWHERE'}")
    if not any_paths:
        print("\n  *** THE AREA RESPONSE CARRIES NO TEE TIMES ***")
        print("  Not one object with a facilityId and a time.date was found at "
              "any depth,\n  in any cluster. The area search returns facilities "
              "for display, not\n  inventory — batching cannot work this way and "
              "the rest of this report\n  is moot. Check `predicate` above "
              "against a facility-page predicate.")
        return 0

    print("\n" + "=" * 78)
    print("PAGE LOADS")
    print("=" * 78)
    loads_area = area.get("page_loads", len(clusters))
    print(f"  facility path: {len(wanted)} page loads (one per course)")
    print(f"  area path:     {loads_area} page loads ({len(clusters)} clusters)")
    if loads_area:
        print(f"  ratio:         {len(wanted) / loads_area:.2f}x fewer loads "
              f"for these clusters")
    print("  (fleet-wide the same clustering is 69 -> 32, a 54% cut; these "
          "clusters are\n   deliberately the extremes, so this ratio is not "
          "the fleet ratio)")

    c_area, e_area = per_course(area)
    c_f1, e_f1 = per_course(fac1)
    c_f3, e_f3 = per_course(fac3)

    print("\n" + "=" * 78)
    print("PER-COURSE SLOTS — fac1 / area2 / fac3   (A/B/A brackets the drift)")
    print("=" * 78)
    print(f"{'course':>34} {'cluster':>22} {'fac1':>6} {'area':>6} {'fac3':>6}  note")
    lost, gained, thin = [], [], []
    for slug in sorted(wanted, key=lambda s: (cluster_of[s], s)):
        f1 = c_f1.get(slug, 0)
        f3 = c_f3.get(slug, 0)
        ar = c_area.get(slug, 0)
        note = ""
        if slug in e_area:
            note = "AREA ERRORED"
            lost.append(slug)
        elif f1 > 0 and f3 > 0 and ar == 0:
            # The gate. Both facility passes found inventory; the area pass
            # found none. That is a dropped course, not an empty one.
            note = "<-- LOST BY AREA"
            lost.append(slug)
        elif ar > 0 and f1 == 0 and f3 == 0:
            note = "area-only (facility found none)"
            gained.append(slug)
        elif f1 > 0 and f3 > 0 and ar < min(f1, f3) * 0.5:
            # Not a hard failure — could be a per-facility display cap in the
            # area view — but a cap that halves inventory is not shippable.
            note = "thin (<50% of both facility passes)"
            thin.append(slug)
        print(f"{slug[:34]:>34} {cluster_of[slug][:22]:>22} "
              f"{f1:>6} {ar:>6} {f3:>6}  {note}")

    tot_f1 = sum(c_f1.get(s, 0) for s in wanted)
    tot_f3 = sum(c_f3.get(s, 0) for s in wanted)
    tot_ar = sum(c_area.get(s, 0) for s in wanted)
    print(f"\n{'TOTAL':>34} {'':>22} {tot_f1:>6} {tot_ar:>6} {tot_f3:>6}")
    if fac1 and fac3:
        drift = abs(tot_f1 - tot_f3)
        base = max(tot_f1, tot_f3) or 1
        print(f"  drift between the two facility passes: {drift} slots "
              f"({drift / base * 100:.0f}%) — any area delta smaller than this "
              f"is noise")

    ans_f1 = {s for s in wanted if c_f1.get(s, 0) > 0}
    ans_f3 = {s for s in wanted if c_f3.get(s, 0) > 0}
    ans_ar = {s for s in wanted if c_area.get(s, 0) > 0}
    stable = ans_f1 & ans_f3
    print(f"\n  courses answering: fac1 {len(ans_f1)}, area {len(ans_ar)}, "
          f"fac3 {len(ans_f3)}, of {len(wanted)}")
    print(f"  stable set (answered in BOTH facility passes): {len(stable)}")

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    if lost:
        print(f"  *** PARITY FAILURE *** {len(lost)} course(s) the facility path "
              f"found and the\n  area path did not: {', '.join(sorted(lost))}")
        print("\n  Do NOT switch golfnow to area searches on this result. In "
              "production this\n  would not go red — browser_golfnow ends every "
              "invocation in `|| true` and\n  d1.sync only deactivates courses "
              "present in the scrape, so these courses\n  would keep serving "
              "stale tee times until they aged out.")
    elif thin:
        print(f"  PARTIAL — every course answered, but {len(thin)} returned "
              f"under half the\n  inventory of both facility passes: "
              f"{', '.join(sorted(thin))}")
        print("\n  That is the signature of a per-facility display cap in the "
              "area view.\n  Check whether a larger pageSize or more pages "
              "recovers them before shipping.")
    else:
        print(f"  PARITY OK — every one of the {len(stable)} courses that "
              f"answered in both\n  facility passes also answered in the area "
              f"pass, and none returned\n  materially less inventory.")
        print(f"\n  On these clusters the area path used {loads_area} page loads "
              f"where the facility\n  path used {len(wanted)}. Fleet-wide that "
              f"clustering is 69 -> 32.")
    if gained:
        print(f"\n  note: {len(gained)} course(s) answered ONLY in the area pass "
              f"({', '.join(sorted(gained))}).\n  Worth a look — the facility "
              f"path may be losing these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
