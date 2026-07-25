"""Why do these five GolfNow facilities return nothing?

WHAT WE ALREADY KNOW
--------------------
The GolfNow browser adapter works. Grandote Peaks (169 slots) and The Ridge at
Castle Pines North (11) are live in D1 through it right now. So the predicate
capture, the in-page replay and the D1 push are all fine, and whatever is wrong
with Black Bear, Desert Hawk, Pelican Lakes, Tamarack and Walking Stick is
specific to those facilities.

`browser_golfnow.run()` collapses every one of those failures into a single
string — "browser status 200" or "browser no search body captured" — which is
enough to know a course is broken and not enough to know why. There are three
candidate explanations and they need completely different fixes, so guessing
between them is how an afternoon disappears:

  1. WRONG SLUG. GolfNow 404s /facility/<id>-<slug>/search when the slug
     segment is not its canonical one. This already bit Black Bear once. A 404
     still renders a page, so the adapter's retry loop sees no predicate and
     reports "no search body captured", which reads like a timing problem.
  2. WRONG FACILITY ID. The search returns tee times, the in-page filter
     `s.facilityId !== fid` drops all of them because GolfNow files the course
     under a different id than the booking URL implies. Reports "status 200"
     with zero rows — indistinguishable from a course with no tee times.
  3. GENUINELY EMPTY. A nine-hole municipal in Limon may simply have nothing
     on GolfNow for the dates we ask about. Nothing to fix; the right response
     is to stop calling it a gap and let the directory card handle it.

WHAT THIS PRINTS
----------------
For each facility, per date: the final URL after redirects (catches 1), whether
a predicate was captured, the raw response status, the TOTAL tee times before
filtering, and a breakdown of every facilityId/name present in the response
with counts (catches 2 — if the course's own name appears under an id that is
not ours, that id is the answer). A run that shows a captured predicate, a 200,
a non-zero total and no matching facility is case 2. Everything zero across
three dates is case 3.

Read only. It POSTs the same predicate the page itself POSTs, from the page's
own origin, exactly as the production adapter does — no extra load on GolfNow
beyond what a visitor browsing those five pages would generate.

    python3 scripts/diag_golfnow.py --slugs walking-stick-golf-course
    python3 scripts/diag_golfnow.py --state CO --offsets 0,1,3
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

sys.path.insert(0, ".")

from scraper.adapters.base import USER_AGENT          # noqa: E402
from scraper.aggregate import load_registry            # noqa: E402
from scraper.browser_golfnow import SEARCH_EP          # noqa: E402

log = logging.getLogger("diag")

# Same replay as production, but it reports what it saw INSTEAD of filtering it
# away. The facility breakdown is the whole point: production's
# `if (fid && s.facilityId !== fid) continue` is exactly where a wrong id
# becomes an empty result with no explanation.
PROBE_JS = r"""
async ([bodyStr, dateStr, fid]) => {
  let body;
  try { body = JSON.parse(bodyStr); } catch (e) { return {error: "bad predicate body"}; }
  body.date = dateStr; body.pageSize = 40; body.teeTimeCount = 40; body.pageNumber = 0;
  let r, j = {};
  try {
    r = await fetch(location.origin + "/api/tee-times/tee-time-search-results",
      {method:"POST", headers:{"Content-Type":"application/json","Accept":"application/json"},
       body: JSON.stringify(body)});
    j = await r.json();
  } catch (e) { return {error: String(e)}; }
  const tt = (j.ttResults && j.ttResults.teeTimes) || [];
  const byFac = {};
  for (const s of tt) {
    const k = s.facilityId + "|" + (s.facilityName || s.courseName || "?");
    byFac[k] = (byFac[k] || 0) + 1;
  }
  return {
    status: r.status,
    total: tt.length,
    mine: tt.filter((s) => s.facilityId === fid).length,
    facilities: Object.entries(byFac).sort((a,b) => b[1]-a[1]).slice(0, 12),
    // The predicate carries the lat/long the page chose for this facility. A
    // radius search centred on the wrong point is a fourth way to get zero.
    center: [body.latitude, body.longitude, body.radius].join(","),
  };
}
"""


def probe(page, c: dict, offsets: list[int], captured: dict) -> dict:
    fid = int(c["ids"]["golfnow_facility_id"])
    gn_slug = c["ids"].get("golfnow_slug") or c["slug"]
    url = f"https://www.golfnow.com/tee-times/facility/{fid}-{gn_slug}/search"
    rec: dict = {"slug": c["slug"], "facility_id": fid, "url": url, "dates": []}

    captured.pop("body", None)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        rec["http"] = resp.status if resp else None
        # A slug mismatch redirects rather than erroring, so the landing URL is
        # the tell — not the status code, which is usually still 200.
        rec["landed"] = page.url
        rec["redirected"] = page.url.rstrip("/") != url.rstrip("/")
        rec["title"] = (page.title() or "")[:80]
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"[:160]
        return rec

    for _ in range(24):
        page.wait_for_timeout(500)
        if captured.get("body"):
            break
    rec["predicate_captured"] = bool(captured.get("body"))
    if not captured.get("body"):
        return rec

    today = dt.date.today()
    for off in offsets:
        d = today + dt.timedelta(days=off)
        date_str = f"{d:%b} {d.day} {d:%Y}"
        try:
            r = page.evaluate(PROBE_JS, [captured["body"], date_str, fid])
        except Exception as e:  # noqa: BLE001
            r = {"error": f"{type(e).__name__}: {e}"[:120]}
        r["date"] = d.isoformat()
        rec["dates"].append(r)
        page.wait_for_timeout(900)
    return rec


def verdict(rec: dict) -> str:
    """The whole point of the run: turn the numbers into the next action."""
    if rec.get("error"):
        return f"PAGE ERROR — {rec['error']}"
    if rec.get("redirected"):
        return ("WRONG SLUG — GolfNow redirected us. Canonical URL is "
                f"{rec.get('landed')}; take the slug from it.")
    if not rec.get("predicate_captured"):
        return ("NO PREDICATE — page never POSTed a search. Either it is not a "
                "search page or it needs longer than 12s.")
    ds = rec.get("dates") or []
    tot = sum(d.get("total") or 0 for d in ds)
    mine = sum(d.get("mine") or 0 for d in ds)
    if mine:
        return f"WORKS — {mine} of {tot} rows are ours; production should have these."
    if not tot:
        return ("GENUINELY EMPTY — GolfNow returned no tee times at all for any "
                "date tested. Not a bug: retag as no-online-booking.")
    others = {}
    for d in ds:
        for k, n in (d.get("facilities") or []):
            others[k] = others.get(k, 0) + n
    top = sorted(others.items(), key=lambda kv: -kv[1])[:4]
    return ("WRONG FACILITY ID — " + str(tot) + " rows came back, none under id "
            + str(rec["facility_id"]) + ". Present instead: "
            + "; ".join(f"{k} ({n})" for k, n in top))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--state", default="CO")
    ap.add_argument("--slugs", help="comma-separated; overrides --state")
    ap.add_argument("--offsets", default="0,1,3")
    ap.add_argument("--out", default="probe-results/golfnow-diag.txt")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    offsets = [int(x) for x in a.offsets.split(",") if x.strip()]
    reg = load_registry(a.registry)
    gn = [c for c in reg if c["platform"] == "golfnow"
          and c["ids"].get("golfnow_facility_id")]
    if a.slugs:
        want = {s.strip() for s in a.slugs.split(",")}
        gn = [c for c in gn if c["slug"] in want]
    else:
        gn = [c for c in gn if c["state"] == a.state]

    print(f"probing {len(gn)} golfnow facilities on dates {offsets}\n", flush=True)

    from playwright.sync_api import sync_playwright
    out: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        captured: dict[str, str] = {}
        page.on("request", lambda r: captured.__setitem__("body", r.post_data or "")
                if (SEARCH_EP in r.url and r.method == "POST") else None)
        for i, c in enumerate(gn):
            if i:
                page.wait_for_timeout(1500)
            rec = probe(page, c, offsets, captured)
            rec["verdict"] = verdict(rec)
            out.append(rec)
            print(f"{c['slug']:<38} {rec['verdict']}", flush=True)
        browser.close()

    lines = [f"golfnow diagnosis — {dt.datetime.now(dt.timezone.utc).isoformat()}",
             f"dates probed: offsets {offsets} from today", ""]
    for rec in out:
        lines.append(f"=== {rec['slug']}  (facility {rec['facility_id']}) ===")
        lines.append(f"  VERDICT: {rec['verdict']}")
        lines.append(f"  url        : {rec['url']}")
        lines.append(f"  landed     : {rec.get('landed')}  redirected={rec.get('redirected')}")
        lines.append(f"  http/title : {rec.get('http')}  {rec.get('title','')!r}")
        lines.append(f"  predicate  : {rec.get('predicate_captured')}")
        for d in rec.get("dates") or []:
            lines.append(f"  {d.get('date')}  status={d.get('status')} "
                         f"total={d.get('total')} mine={d.get('mine')} "
                         f"center={d.get('center')}"
                         + (f" error={d['error']}" if d.get("error") else ""))
            for k, n in (d.get("facilities") or [])[:8]:
                lines.append(f"      {n:>4}  {k}")
        lines.append("")

    import pathlib
    p = pathlib.Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))
    p.with_suffix(".json").write_text(json.dumps(out, indent=1))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
