"""Per-state coverage: where every course in a state ends up, and why.

WHY THIS EXISTS
---------------
Until now "what's broken" has been answered per COURSE — a list of named
misses carried forward by hand in the changelog, re-measured by a bespoke
probe each time. That does not scale past two states. At fifty it is not a
list anyone reads, and the same three structural failures (no booking URL in
the source data, a platform we have not implemented, identifiers we never
captured) get rediscovered one course at a time.

So this reports a FUNNEL per state instead of a list of names. Every course in
a state's CSV lands in exactly one bucket, the buckets are ordered by how far
the course got, and the fix for a bucket is the same for every course in it.
Adding Utah means adding a CSV and reading one more row of this table.

THE FUNNEL, in order. A venue is counted once, in the furthest bucket any of
its booking sources reached:

  live          rows in D1 right now — a golfer can see and book it
  silent        we believe we can scrape it, but it returns nothing. THE
                BUCKET THAT MATTERS: the registry calls these ready, so
                nothing flags them, and they are indistinguishable from a
                course that is simply closed for the season.
  needs_ids     right platform, identifiers we have not captured
  experimental  platform recognised, adapter not production (golfnow, ezlinks)
  unsupported   no adapter is possible today (dead tenant, login-gated portal)
  no_platform   the source CSV has no booking platform for it
  no_booking    the source CSV says the course has no online booking at all

The last two never reach registry.json at all — build_registry skips them —
which is exactly why they have been invisible. They are not bugs, but they
are the honest denominator: "we cover 205 courses" means nothing without
"out of how many".

VENUE vs SOURCE. Coverage is counted per VENUE (one physical golf course),
not per booking source, because a golfer cares about the course. A venue with
a native engine plus a GolfNow overflow listing is one venue, two sources, and
is live if either source produces rows.

Offline by default: CSVs + registry.json only. Pass --d1 to join live row
counts from D1 (needs the Cloudflare env vars), or --counts FILE to feed in a
snapshot of `SELECT course_slug, COUNT(*) ... GROUP BY course_slug` as JSON.
Report only — nothing here writes to the CSV, the registry, or D1.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_registry import SOURCES, slugify  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ordered furthest-along first; a venue is reported in the best bucket any of
# its sources achieved.
BUCKETS = ["live", "silent", "needs_ids", "experimental", "unsupported",
           "no_platform", "no_booking", "private"]

BUCKET_BLURB = {
    "live": "serving tee times now",
    "silent": "believed scrapable, returning nothing",
    "needs_ids": "right platform, identifiers not captured",
    "experimental": "adapter exists but is not production",
    "unsupported": "no adapter possible today",
    "no_platform": "no booking platform in the source data",
    "no_booking": "playable, but no online booking in the source data",
    "private": "members only — out of scope, not a gap",
}

# Private and military clubs are not a coverage failure; a golfer cannot book
# them at any price. They are excluded from the denominator so the percentage
# means "of the courses you could actually sell, how many do we serve".
OUT_OF_SCOPE_TYPES = {"private", "military"}

# Which registry statuses map to which bucket before liveness is considered.
STATUS_BUCKET = {
    "ready": "silent",           # promoted to live if D1 has rows
    "experimental": "experimental",
    "needs_ids": "needs_ids",
    "unsupported": "unsupported",
}


def load_registry() -> list[dict]:
    with open(os.path.join(ROOT, "registry.json")) as fh:
        doc = json.load(fh)
    return doc["courses"] if isinstance(doc, dict) else doc


def load_counts(args) -> dict[str, int]:
    """id -> active row count, or {} when running offline.

    The id may be a source slug (what D1 stores per scrape) or a venue_id
    (what /api/courses hands out). build() looks up both, so a snapshot taken
    from either side works.

    Accepts a TSV of `state<TAB>slug<TAB>count` — the shape you get by pasting
    /api/courses output — or JSON, either {slug: n} or [{course_slug, n}].
    """
    if args.counts:
        if args.counts.endswith((".tsv", ".txt")):
            out = {}
            with open(args.counts) as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2:
                        continue
                    slug, n = parts[-2], parts[-1]
                    out[slug] = out.get(slug, 0) + int(n)
            return out
        with open(args.counts) as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            return {k: int(v) for k, v in doc.items()}
        return {r["course_slug"]: int(r["n"]) for r in doc}
    if not args.d1:
        return {}
    # VPS cutover (2026-08-11): the live store is Postgres behind
    # api.oneteeapp.com, not D1. VPS_ONLY=1 (set by the workflow, same switch
    # as scraper.d1's CLI) routes the join there; the retired D1Rest path had
    # this report failing daily and probe-results/state-status.json frozen at
    # its last pre-cutover success — which is how a permanently closed course
    # (Del Lago) stayed "live" long enough to fail the directory gate.
    import os
    if os.environ.get("VPS_ONLY") == "1":
        from scraper.d1 import HttpBackend
        db = HttpBackend()
    else:
        from scraper.d1 import D1Rest
        db = D1Rest()
    rows = db.execute(
        "SELECT course_slug, COUNT(*) n FROM tee_times "
        "WHERE active = 1 GROUP BY course_slug")
    return {r["course_slug"]: int(r["n"]) for r in rows}


def build(counts: dict[str, int]) -> dict:
    """The whole report, as plain data so a dashboard can render it."""
    reg = load_registry()
    by_state_slug = {}
    for c in reg:
        by_state_slug.setdefault(c["state"], {})[slugify(c["name"])] = c
    # Registry sources grouped by venue, so a venue is judged on its best source.
    sources_by_venue: dict[tuple, list] = {}
    for c in reg:
        sources_by_venue.setdefault((c["state"], c["venue_id"]), []).append(c)

    # Integrity tracking: every id we successfully attributed to a CSV venue.
    # Whatever is left over in `counts` afterwards is serving tee times that
    # this report cannot explain — registry drift, a retired slug still in D1,
    # or a course that was never in the source CSV.
    matched: set = set()
    consumed: set = set()

    states = []
    for src, state in SOURCES:
        path = os.path.join(ROOT, src)
        if not os.path.exists(path):
            continue

        # 1. Everything the source data knows about this state, deduped to
        #    venues by the same slugify() build_registry uses, so the CSV and
        #    the registry are talking about the same thing.
        venues: "OrderedDict[str, dict]" = OrderedDict()
        with open(path) as fh:
            for row in csv.DictReader(fh):
                vb = slugify(row["Course Name"])
                v = venues.setdefault(vb, {
                    "slug": vb, "name": row["Course Name"],
                    "city": row["City"], "rows": []})
                v["rows"].append(row)

        # 2. Bucket each venue by the furthest any of its sources got.
        placed: dict[str, list] = {b: [] for b in BUCKETS}
        for vb, v in venues.items():
            regs = sources_by_venue.get((state, vb), [])
            if regs:
                consumed.add((state, vb))
            if not regs:
                # Never entered the registry. Distinguish "the directory says
                # this course has no online booking" (a fact about the world)
                # from "we have no platform for it" (a gap in our data).
                any_booking = any(r["Online Booking"] == "yes" for r in v["rows"])
                private = all(r.get("Type", "").strip().lower()
                              in OUT_OF_SCOPE_TYPES for r in v["rows"])
                # private wins over any_booking: a members-only club that
                # happens to book online is still out of scope, and counting
                # it as "no_platform" inflated both the addressable
                # denominator and the blocker table.
                bucket = ("private" if private
                          else "no_platform" if any_booking else "no_booking")
                placed[bucket].append({
                    "slug": vb, "venue_id": vb,
                    "name": v["name"], "city": v["city"],
                    "platform": (v["rows"][0].get("Booking Platform") or ""),
                    "rows": 0, "why": BUCKET_BLURB[bucket]})
                continue

            best, best_src, live_rows = len(BUCKETS), None, 0
            for c in regs:
                # D1 keys rows by source slug; /api/courses reports by venue.
                # Either identifies the same scrape, so accept both.
                n = counts.get(c["slug"], 0) or counts.get(c.get("venue_id"), 0)
                matched.update({c["slug"], c.get("venue_id")})
                b = STATUS_BUCKET.get(c["status"], "unsupported")
                # Rows in D1 beat any status the registry claims. An
                # `experimental` or `needs_ids` source that is nonetheless
                # producing tee times is live to a golfer, and calling it
                # anything else overstates how much work is left.
                if n > 0:
                    b = "live"
                live_rows += n
                if BUCKETS.index(b) < best:
                    best, best_src = BUCKETS.index(b), c
            bucket = BUCKETS[best]
            placed[bucket].append({
                # slug is the best SOURCE's slug (may be a supplement like
                # "foo-golfnow"); venue_id is the venue key the directory
                # joins on — verify_directory's live-vs-tag check needs it.
                "slug": best_src["slug"],
                "venue_id": best_src.get("venue_id") or best_src["slug"],
                "name": best_src["name"],
                "city": best_src["city"],
                "platform": best_src["platform"],
                "sources": len(regs), "rows": live_rows,
                "why": BUCKET_BLURB[bucket]})

        states.append({
            "state": state, "source": src,
            "venues": len(venues),
            "addressable": len(venues) - len(placed["private"]),
            "counts": {b: len(placed[b]) for b in BUCKETS},
            "detail": placed,
            "platforms": dict(Counter(
                c["platform"] for c in reg if c["state"] == state)),
        })

    # THE MACRO VIEW. Buckets tell you how many courses are stuck; blockers
    # tell you what to fix. Each blocker is (bucket, platform) rolled up
    # across every state, because that is the unit of work that actually
    # moves the number: one chronogolf id-capture pass unlocks every
    # chronogolf course in needs_ids, in every state, at once. This list is
    # what stays readable at fifty states — it grows with the number of
    # PLATFORMS, which is bounded, not with the number of courses, which is not.
    blockers: dict[tuple, dict] = {}
    for s in states:
        for b in BUCKETS:
            if b in ("live", "private"):
                continue
            for it in s["detail"][b]:
                k = (b, it.get("platform") or "—")
                e = blockers.setdefault(k, {
                    "bucket": b, "platform": k[1], "courses": 0,
                    "states": {}, "why": BUCKET_BLURB[b]})
                e["courses"] += 1
                e["states"][s["state"]] = e["states"].get(s["state"], 0) + 1
    blocker_list = sorted(blockers.values(),
                          key=lambda e: (BUCKETS.index(e["bucket"]),
                                         -e["courses"]))

    orphan_reg = [{"state": k[0], "venue_id": k[1],
                   "name": v[0]["name"], "platform": v[0]["platform"],
                   "rows": counts.get(v[0]["slug"], 0)
                          or counts.get(v[0].get("venue_id"), 0)}
                  for k, v in sorted(sources_by_venue.items())
                  if k not in consumed]
    orphan_live = sorted(k for k, n in counts.items()
                         if n and k and k not in matched)

    return {"has_live_data": bool(counts), "states": states,
            "blockers": blocker_list,
            "orphan_registry": orphan_reg, "orphan_live": orphan_live}


def render(report: dict) -> None:
    live_known = report["has_live_data"]
    print("=" * 74)
    print("PER-STATE COVERAGE" + ("" if live_known else
          "   (offline — no D1 join, 'live' unknown so all ready courses "
          "show as silent)"))
    print("=" * 74)

    def pct(s):
        d = s["addressable"]
        return (100.0 * s["counts"]["live"] / d) if d else 0.0

    hdr = (f"{'state':<6}{'bookable':>9}{'live':>6}{'cover':>7}"
           + "".join(f"{b:>13}" for b in BUCKETS[1:]))
    print(hdr)
    print("-" * len(hdr))
    tot = Counter()
    for s in report["states"]:
        line = (f"{s['state']:<6}{s['addressable']:>9}"
                f"{s['counts']['live']:>6}{pct(s):>6.0f}%")
        for b in BUCKETS[1:]:
            line += f"{s['counts'][b]:>13}"
            tot[b] += s["counts"][b]
        tot["live"] += s["counts"]["live"]
        tot["addressable"] += s["addressable"]
        print(line)
    print("-" * len(hdr))
    allpct = 100.0 * tot["live"] / tot["addressable"] if tot["addressable"] else 0
    print(f"{'ALL':<6}{tot['addressable']:>9}{tot['live']:>6}{allpct:>6.0f}%"
          + "".join(f"{tot[b]:>13}" for b in BUCKETS[1:]))
    print("\n  bookable = every course in the state's directory minus private "
          "and military clubs,\n  which no aggregator can sell. cover = live / "
          "bookable.")

    print("\n" + "=" * 74)
    print("WHAT'S BLOCKING — one row per fix, sorted by how far along it is")
    print("=" * 74)
    print(f"  {'bucket':<13}{'platform':<22}{'courses':>8}   states")
    for e in report["blockers"]:
        spread = " ".join(f"{k}:{v}" for k, v in sorted(e["states"].items()))
        print(f"  {e['bucket']:<13}{e['platform'][:21]:<22}"
              f"{e['courses']:>8}   {spread}")

    for s in report["states"]:
        print("\n" + "=" * 74)
        print(f"{s['state']}  —  {s['addressable']} bookable courses "
              f"({s['venues']} in {s['source']}, "
              f"{s['counts']['private']} private), "
              f"{s['counts']['live']} live ({pct(s):.0f}%)")
        print("=" * 74)
        for b in BUCKETS:
            items = s["detail"][b]
            if not items or b in ("live", "private"):
                continue
            print(f"\n  {b.upper()}  ({len(items)}) — {BUCKET_BLURB[b]}")
            for it in sorted(items, key=lambda i: i["name"]):
                plat = it.get("platform") or "—"
                print(f"     {it['name'][:44]:<44} {plat:<14} {it['city']}")

    # Integrity: things this report could not reconcile. These are the macro
    # bugs — at fifty states nobody reads a course list, but "12 registry
    # entries no longer match their CSV" is a number worth alarming on.
    if report["orphan_registry"]:
        print("\n" + "=" * 74)
        print(f"REGISTRY DRIFT ({len(report['orphan_registry'])}) — in "
              "registry.json, no matching course in the state CSV")
        print("=" * 74)
        for o in report["orphan_registry"]:
            print(f"  {o['state']}  {o['name'][:44]:<44} {o['platform']:<14} "
                  f"{o['rows']} rows")
    if report["orphan_live"]:
        print("\n" + "=" * 74)
        print(f"UNATTRIBUTED LIVE ({len(report['orphan_live'])}) — serving "
              "tee times, not traceable to a CSV course")
        print("=" * 74)
        for k in report["orphan_live"]:
            print(f"  {k}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--d1", action="store_true",
                    help="join live row counts from D1 (needs CF env vars)")
    ap.add_argument("--counts", help="JSON snapshot of active rows per slug")
    ap.add_argument("--json", help="write the report as JSON to this path")
    a = ap.parse_args()

    report = build(load_counts(a))
    render(report)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(report, fh, indent=1)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
