"""GolfNow via AREA searches instead of one page load per facility.

EXPERIMENTAL — not wired into any production workflow. Its only caller is
.github/workflows/probe-golfnow-batching.yml, which compares what this returns
against what the unmodified scraper.browser_golfnow returns for the same date.
Nothing here writes to the database.

WHY THIS MIGHT WORK. GolfNow's tee-time predicate is an area query — it carries
latitude, longitude and radius, and the facility page merely narrows it to one
facilityId. Probed live 2026-08-14: `facilityIds` is ignored while `facilityId`
is set, and `facilityId: 0` under `searchType: "Facility"` is a 500, so several
facilities cannot be folded into one FACILITY search. But the site's own area
search (`searchType: "GeoLocation"`) renders 17 distinct facilities in a single
page load, and greedy set-cover says 35-mile circles cover all 69 of our courses
in 32 searches instead of 69.

WHY IT MIGHT NOT. Hand-building an area predicate does not work: `view:
"Grouping"` and `view: "List"` return 200 with an empty teeTimes array, and
`view: "Course"` — what the site actually uses — returns 500 when scripted. So
this does what the facility fetcher already does and what is known to work:
loads the real page, captures the body THE PAGE ITSELF posts, and replays it
with only the date and pageNumber changed. Every other field is left exactly as
captured, because the 500s above are what happens when you edit fields you do
not understand.

THE OPEN QUESTION THIS EXISTS TO ANSWER. The rendered area card shows a course's
name, city and review count — not its tee times. Whether the JSON behind it
carries per-facility slots is unknown, and if it does not, batching buys nothing.
So the harvester does NOT assume a response shape: it walks the whole document
for objects that look like a tee time (a numeric facilityId plus time.date) and
reports the JSON paths where it found them. An unknown shape comes back as a
diagnostic, not as zero slots.

    python -m scraper.browser_golfnow_area --clusters clusters.json \
        --date 2026-08-19 --out probe-out/area.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

from .adapters.base import USER_AGENT
from .browser_golfnow import SEARCH_EP, _slots_to_teetimes

log = logging.getLogger("teetime")

AREA_URL = ("https://www.golfnow.com/tee-times/search"
            "#facilitytype=GolfCourse&latitude={lat}&longitude={lng}&radius={r}")

# Replay the page's own area predicate, changing ONLY date and pageNumber.
#
# The harvest is deliberately shape-agnostic. The facility fetcher can read
# ttResults.teeTimes because it knows the Grouping-view shape; the area response
# may nest slots under a per-facility grouping instead, and guessing wrong would
# look exactly like "these courses have no tee times" — the same failure this
# codebase has already shipped twice (a non-JSON body swallowed into an empty
# object, and pageNumber pinned at 0 truncating to the 40 earliest slots). So we
# walk the document, take every object carrying a numeric facilityId and a
# time.date, and report the paths we found them at.
AREA_JS = r"""
async ([bodyStr, dateStr, wanted, maxPages]) => {
  let body;
  try { body = JSON.parse(bodyStr); } catch (e) { return {error: "bad body"}; }
  const want = new Set(wanted || []);
  const money = (m) => (m && typeof m.value === "number") ? m.value : null;
  const paths = {};
  const seen = new Set();
  const slots = [];
  let status = 0, total = null, pages = 0, rawSeen = 0;

  const harvest = (node, path, depth) => {
    if (!node || depth > 8) return 0;
    if (Array.isArray(node)) {
      let n = 0;
      for (const x of node) n += harvest(x, path, depth + 1);
      return n;
    }
    if (typeof node !== "object") return 0;
    if (typeof node.facilityId === "number" && node.time && node.time.date) {
      paths[path || "(root)"] = (paths[path || "(root)"] || 0) + 1;
      // Dedupe across pages AND across per-facility groupings, which may repeat
      // the same slot. detailUrl is the stable per-slot identity; fall back to
      // facility+time when it is absent.
      const key = node.detailUrl || (node.facilityId + "|" + node.time.date);
      if (seen.has(key)) return 1;
      seen.add(key);
      if (want.size === 0 || want.has(node.facilityId)) {
        slots.push({
          facilityId: node.facilityId,
          date: node.time.date,
          playerRule: node.playerRule,
          detailUrl: node.detailUrl,
          display: money(node.displayRate),
          minRate: money(node.minTeeTimeRate),
          maxRate: money(node.maxTeeTimeRate),
          rates: (node.teeTimeRates || []).map((x) => ({
            holes: x.holeCount,
            greens: x.singlePlayerPrice ? money(x.singlePlayerPrice.greensFees) : null,
          })),
        });
      }
      return 1;
    }
    let n = 0;
    for (const k of Object.keys(node)) {
      n += harvest(node[k], path ? path + "." + k : k, depth + 1);
    }
    return n;
  };

  for (let pageNumber = 0; pageNumber < maxPages; pageNumber++) {
    body.date = dateStr;
    body.pageNumber = pageNumber;
    const r = await fetch(location.origin + "/api/tee-times/tee-time-search-results",
      {method: "POST",
       headers: {"Content-Type": "application/json", "Accept": "application/json"},
       body: JSON.stringify(body)});
    status = r.status;
    const text = await r.text();
    if (!text) {
      // An empty body with a 5xx is the signature of a predicate the server
      // would not accept. Report it as a failure, never as zero inventory.
      return {status: status, empty_body: true, page: pageNumber,
              pages: pages, slots: slots, paths: paths, total: total};
    }
    let j;
    try { j = JSON.parse(text); }
    catch (e) {
      return {status: status, parse_failed: true, bytes: text.length,
              content_type: r.headers.get("content-type") || "",
              page: pageNumber, pages: pages, slots: slots, paths: paths};
    }
    if (typeof j.total === "number") total = j.total;
    const before = slots.length;
    const raw = harvest(j, "", 0);
    rawSeen += raw;
    pages = pageNumber + 1;
    // Stop on a page that carried no tee-time-shaped object at all, or that
    // added nothing new. Judged on RAW objects seen, not on slots kept: a page
    // full of facilities we do not track must still advance the loop.
    if (raw === 0) break;
    if (slots.length === before && pageNumber > 0) break;
  }
  return {status: status, pages: pages, total: total, raw_seen: rawSeen,
          slots: slots, paths: paths};
}
"""

# Only the scope fields, never the whole predicate.
_SCOPE_KEYS = ("searchType", "view", "facilityId", "facilityIds", "radius",
               "pageSize", "teeTimeCount", "sortBy", "sortByRollup",
               "facilityType", "disableCourseView")


def _scope(body: str) -> dict:
    try:
        b = json.loads(body)
    except Exception:  # noqa: BLE001
        return {"unreadable": True}
    return {k: b.get(k) for k in _SCOPE_KEYS if k in b}


def run(date: dt.date, clusters_path: str, out_path: str,
        max_pages: int = 15) -> dict:
    from playwright.sync_api import sync_playwright

    plan = json.loads(pathlib.Path(clusters_path).read_text())
    clusters = plan["clusters"]
    date_str = f"{date:%b} {date.day} {date:%Y}"   # "Aug 19 2026" (no zero-pad)
    n_courses = sum(len(c["members"]) for c in clusters)
    log.info("area-fetching %d clusters covering %d courses for %s",
             len(clusters), n_courses, date)

    tee_times, errors, diags = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        captured: dict[str, str] = {}

        def on_request(req):
            if SEARCH_EP in req.url and req.method == "POST":
                try:
                    captured["body"] = req.post_data or ""
                except Exception:  # noqa: BLE001
                    pass

        page.on("request", on_request)

        for i, cl in enumerate(clusters):
            by_fid = {int(m["facility_id"]): m for m in cl["members"]}
            wanted = sorted(by_fid)
            label = f"{cl['centre_name'][:30]} ({len(wanted)})"
            if i:
                page.wait_for_timeout(1500)
            last, got = None, False
            for attempt in range(3):
                try:
                    captured.pop("body", None)
                    page.goto(AREA_URL.format(lat=cl["lat"], lng=cl["lng"],
                                              r=int(cl["radius_mi"])),
                              wait_until="domcontentloaded", timeout=45000)
                    for _ in range(75):          # ~15s, checking first
                        if captured.get("body"):
                            break
                        page.wait_for_timeout(200)
                    if not captured.get("body"):
                        last = "no area search body captured"
                        raise RuntimeError(last)
                    scope = _scope(captured["body"])
                    r = page.evaluate(AREA_JS,
                                      [captured["body"], date_str, wanted, max_pages])
                    last = f"status {r.get('status')}"
                    if r.get("parse_failed"):
                        last = (f"status {r.get('status')} but the body was not "
                                f"JSON ({r.get('content_type', '?')[:40]}, "
                                f"{r.get('bytes')} bytes)")
                    elif r.get("empty_body"):
                        last = (f"status {r.get('status')} with an empty body on "
                                f"page {r.get('page')}")
                    elif r.get("status") == 200:
                        slots = r.get("slots") or []
                        per: dict[int, list] = {}
                        for s in slots:
                            per.setdefault(s["facilityId"], []).append(s)
                        for fid, mem in by_fid.items():
                            tee_times.extend(_slots_to_teetimes(mem, per.get(fid, [])))
                        diags.append({
                            "cluster": cl["centre_name"],
                            "centre_slug": cl["centre_slug"],
                            "members": len(wanted),
                            "predicate_scope": scope,
                            "pages": r.get("pages"),
                            "server_total": r.get("total"),
                            "raw_tee_time_objects_seen": r.get("raw_seen"),
                            "kept_slots": len(slots),
                            "facilities_answered": len(per),
                            "harvest_paths": r.get("paths"),
                        })
                        log.info("  %-34s %d slots across %d/%d facilities "
                                 "(server total %s, %s pages)",
                                 label, len(slots), len(per), len(wanted),
                                 r.get("total"), r.get("pages"))
                        got = True
                        break
                except Exception as e:  # noqa: BLE001
                    last = last or f"{type(e).__name__}"
                page.wait_for_timeout(2500 * (attempt + 1))
            if not got:
                # Attribute the failure to EVERY member, so the report shows a
                # missing course rather than a course with zero inventory.
                for mem in by_fid.values():
                    errors.append({"course": mem["slug"], "platform": "golfnow",
                                   "error": f"area {last}"})
                log.info("  %-34s ERROR %s", label, last)
        browser.close()

    doc = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "date": date.isoformat(),
        "mode": "area",
        "clusters_queried": len(clusters),
        "courses_covered": n_courses,
        "page_loads": len(clusters),
        "tee_times": [t.to_dict() for t in tee_times],
        "errors": errors,
        "diagnostics": diags,
    }
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    log.info("wrote %s (%d tee times, %d errors, %d page loads for %d courses)",
             out, len(tee_times), len(errors), len(clusters), n_courses)
    return doc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="GolfNow area-search fetcher (probe)")
    p.add_argument("--date",
                   default=(dt.date.today() + dt.timedelta(days=5)).isoformat())
    p.add_argument("--clusters", default="clusters.json")
    p.add_argument("--max-pages", type=int, default=15)
    p.add_argument("--out", default="probe-out/gn_area.json")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(dt.date.fromisoformat(a.date), a.clusters, a.out, a.max_pages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
