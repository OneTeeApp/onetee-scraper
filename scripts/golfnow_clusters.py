"""Group GolfNow courses into area searches — the plan for batching them.

GolfNow's tee-time predicate is an AREA query (latitude / longitude / radius)
that the facility page then narrows to one facilityId. Probed 2026-08-14:
`facilityIds` is ignored while `facilityId` is set, and `facilityId: 0` under
`searchType: "Facility"` is a 500 — so several facilities cannot be folded into
one FACILITY search. What does work is the site's own area search, which renders
17 distinct facilities in a single page load.

This module answers the arithmetic question that follows: if one page load can
cover every course within R miles of a point, how few loads cover all 69?

    python -m scripts.golfnow_clusters --radius 35
    python -m scripts.golfnow_clusters --radius 35 --out clusters.json

Greedy set-cover, with the candidate centres being the course coordinates
themselves rather than an arbitrary grid — a centre that is a real course is one
we know GolfNow will accept as a search origin, and it keeps every member well
inside the radius instead of on its rim.

The result is a PLAN, not a saving. An area search that covers one course costs
exactly what the facility search it replaces costs; only the multi-member
clusters are a win, and at 35 miles roughly half the fleet sits in singletons.
Coordinates come from directory.json, which carries lat/lng for every venue.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

EARTH_MI = 3958.8


def haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = (math.radians(x) for x in (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * EARTH_MI * math.asin(math.sqrt(h))


def load_courses(registry_path: str, directory_path: str) -> list[dict]:
    """GolfNow courses that have BOTH a facility id and a coordinate."""
    reg = json.loads(pathlib.Path(registry_path).read_text())["courses"]
    dirj = json.loads(pathlib.Path(directory_path).read_text())["courses"]
    geo = {c["venue_id"]: (c.get("lat"), c.get("lng")) for c in dirj}
    out, skipped = [], []
    for c in reg:
        if c.get("platform") != "golfnow":
            continue
        fid = (c.get("ids") or {}).get("golfnow_facility_id")
        if not fid:
            continue
        lat, lng = geo.get(c.get("venue_id"), (None, None))
        if lat is None or lng is None:
            # Not silently dropped: a course with no coordinate cannot be put in
            # a cluster, and pretending the fleet is smaller than it is would
            # overstate the saving. It falls back to a per-facility fetch.
            skipped.append(c["slug"])
            continue
        # Carries every field adapters.base.base_tee_time reads — platform and
        # slug are required there, the rest optional — so a cluster member can
        # be handed straight to it without a second registry lookup.
        out.append({"slug": c["slug"], "venue_id": c["venue_id"],
                    "facility_id": int(fid),
                    "golfnow_slug": (c.get("ids") or {}).get("golfnow_slug")
                    or c["venue_id"],
                    "name": c.get("name") or c["slug"],
                    "display_name": c.get("display_name", ""),
                    "platform": c["platform"],
                    "booking_url": c.get("booking_url", ""),
                    "source_role": c.get("source_role", "primary"),
                    "state": c.get("state", ""), "city": c.get("city", ""),
                    "lat": float(lat), "lng": float(lng)})
    if skipped:
        print(f"note: {len(skipped)} golfnow courses have no coordinate and "
              f"cannot be clustered: {', '.join(sorted(skipped))}",
              file=sys.stderr)
    return out


def cluster(courses: list[dict], radius_mi: float) -> list[dict]:
    """Greedy set-cover. Returns clusters ordered largest-first."""
    pts = [(c["lat"], c["lng"]) for c in courses]
    # Precompute the coverage set of every candidate centre once. O(n^2) on 69
    # courses is nothing, and it keeps the greedy loop honest.
    covers = [{j for j in range(len(courses))
               if haversine_mi(pts[i], pts[j]) <= radius_mi}
              for i in range(len(courses))]
    remaining = set(range(len(courses)))
    picked: list[dict] = []
    while remaining:
        best_i, best_cov = -1, set()
        for i in range(len(courses)):
            cov = covers[i] & remaining
            if len(cov) > len(best_cov):
                best_i, best_cov = i, cov
        if best_i < 0:                      # unreachable: a point always covers itself
            break
        centre = courses[best_i]
        picked.append({
            "centre_slug": centre["slug"],
            "centre_name": centre["name"],
            "lat": centre["lat"], "lng": centre["lng"],
            "radius_mi": radius_mi,
            "members": [courses[j] for j in sorted(best_cov)],
        })
        remaining -= best_cov
    picked.sort(key=lambda c: len(c["members"]), reverse=True)
    return picked


def select_for_probe(clusters: list[dict], top: int) -> list[dict]:
    """The N biggest clusters plus one singleton as a control.

    The singleton matters: it is the case where batching provably cannot help,
    so if the area path loses inventory THERE, the cause is the area path
    itself and not the clustering.
    """
    chosen = clusters[:top]
    singles = [c for c in clusters if len(c["members"]) == 1]
    if singles and not any(len(c["members"]) == 1 for c in chosen):
        chosen = chosen + [singles[0]]
    return chosen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--directory", default="directory.json")
    p.add_argument("--radius", type=float, default=35.0)
    p.add_argument("--top", type=int, default=0,
                   help="write only the N biggest clusters + a singleton control")
    p.add_argument("--out", help="write clusters as JSON here")
    a = p.parse_args(argv)

    courses = load_courses(a.registry, a.directory)
    clusters = cluster(courses, a.radius)
    n_multi = sum(1 for c in clusters if len(c["members"]) > 1)
    print(f"{len(courses)} golfnow courses -> {len(clusters)} area searches at "
          f"{a.radius:g} mi ({n_multi} multi-course, "
          f"{len(clusters) - n_multi} singletons)")
    for c in clusters[:12]:
        print(f"  {len(c['members']):>3} {c['centre_name'][:38]:<38} "
              f"{c['lat']:.4f},{c['lng']:.4f}")
    if len(clusters) > 12:
        print(f"  … {len(clusters) - 12} more")

    chosen = select_for_probe(clusters, a.top) if a.top else clusters
    if a.top:
        print(f"selected {len(chosen)} clusters for the probe: "
              + ", ".join(f"{c['centre_name'][:24]}({len(c['members'])})"
                          for c in chosen))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(
            {"radius_mi": a.radius, "clusters": chosen}, indent=2))
        print(f"wrote {a.out} ({len(chosen)} clusters, "
              f"{sum(len(c['members']) for c in chosen)} courses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
