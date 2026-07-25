"""Is the 404 about the facility, or about the page we asked from?

THE QUESTION
------------
scripts/diag_golfnow.py established that four Colorado facilities — Black Bear
(2484), Desert Hawk (5635), Tamarack (16424), Walking Stick (2724) — POST a
predicate to /api/tee-times/tee-time-search-results and get back HTTP 404
carrying GolfNow's SPA "Page Not Found" document, while eight other facilities
in the same browser session, same endpoint, same headers, get 200 and JSON.

The predicates are structurally identical. All 46 fields match across the two
groups except the ones that identify the course: facilityId, latitude,
longitude, and a timestamp. So the body is not obviously malformed, and the
session is demonstrably fine, which leaves exactly two possibilities:

  A. THE FACILITY. GolfNow's search API genuinely has nothing indexed under
     those ids and answers 404 for them from anywhere. Then these are not
     broken scrapes, they are courses GolfNow lists but does not sell, and the
     honest fix is to retag them rather than keep them in the gap list.

  B. THE PAGE. Something about those four facility pages leaves the session in
     a state the API rejects — a cookie not set, a market not resolved, a
     bootstrap that did not finish. Then production could load a facility page
     that works and ask about the others from there.

These have completely different answers, and no amount of staring at the
predicate distinguishes them.

THE EXPERIMENT
--------------
Load ONE facility page known to work. From it, replay each of the twelve
recorded predicates VERBATIM — the exact bytes that 404'd, reissued from a
session that is not 404ing. Same endpoint, same headers, same body.

  404 again  -> possibility A. The body/facility is the trigger.
  200 now    -> possibility B. The page we asked from is the trigger.

Twelve requests, one page load. No credentials, no challenge solving, nothing
a visitor clicking through GolfNow would not also send.

    python3 scripts/crosscheck_golfnow.py
    python3 scripts/crosscheck_golfnow.py --host arrowhead-golf-club-golfnow
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, ".")

from scraper.adapters.base import USER_AGENT          # noqa: E402

# Replay only. It does not touch the predicate beyond the date, because the
# point is to change as little as possible from the request that failed.
REPLAY_JS = r"""
async ([bodyStr, dateStr]) => {
  let body;
  try { body = JSON.parse(bodyStr); } catch (e) { return {error: "bad predicate"}; }
  if (dateStr) body.date = dateStr;
  let r, text;
  try {
    r = await fetch(location.origin + "/api/tee-times/tee-time-search-results",
      {method:"POST", headers:{"Content-Type":"application/json","Accept":"application/json"},
       body: JSON.stringify(body)});
    text = await r.text();
  } catch (e) { return {error: "fetch: " + String(e)}; }
  const out = {status: r.status,
               content_type: r.headers.get("content-type") || "",
               bytes: text.length,
               asked_facility: body.facilityId};
  try {
    const j = JSON.parse(text);
    const tt = (j.ttResults && j.ttResults.teeTimes) || [];
    out.total = tt.length;
    out.mine = tt.filter((s) => s.facilityId === body.facilityId).length;
  } catch (e) {
    out.not_json = true;
    out.title = (text.match(/<title[^>]*>([\s\S]{0,120}?)<\/title>/i) || [,""])[1].trim();
  }
  return out;
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", default="probe-results/golfnow-diag.json",
                    help="output of diag_golfnow.py; supplies the predicates")
    ap.add_argument("--host", default="cedaredge-golf-club-golfnow",
                    help="facility page to ask FROM (must be one that works)")
    ap.add_argument("--out", default="probe-results/golfnow-crosscheck.txt")
    a = ap.parse_args()

    diag = json.loads(pathlib.Path(a.diag).read_text())
    have = [r for r in diag if r.get("predicate")]
    host = next((r for r in diag if r["slug"] == a.host), None)
    if host is None or not host.get("url"):
        print(f"no record for host {a.host} in {a.diag}")
        return 2

    d = dt.date.today() + dt.timedelta(days=1)
    date_str = f"{d:%b} {d.day} {d:%Y}"
    print(f"asking from {host['url']}\nreplaying {len(have)} predicates for {date_str}\n",
          flush=True)

    from playwright.sync_api import sync_playwright
    rows = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(host["url"], wait_until="domcontentloaded", timeout=45000)
        # Let the page finish bootstrapping — whatever state the API wants, it
        # is set up by the page's own first search, not by the navigation.
        page.wait_for_timeout(9000)
        landed = page.url
        for r in have:
            try:
                res = page.evaluate(REPLAY_JS, [r["predicate"], date_str])
            except Exception as e:  # noqa: BLE001
                res = {"error": f"{type(e).__name__}: {e}"[:120]}
            res["slug"] = r["slug"]
            res["facility_id"] = r["facility_id"]
            res["diag_verdict"] = (r.get("verdict") or "").split(" —")[0]
            rows.append(res)
            print(f"  {r['slug']:<40} status={res.get('status')} "
                  f"total={res.get('total')} mine={res.get('mine')} "
                  f"{res.get('title') or ''}", flush=True)
            page.wait_for_timeout(1200)
        browser.close()

    failed_before = [x for x in rows if x["diag_verdict"] == "NO VALID RESPONSE"]
    still_404 = [x for x in failed_before if x.get("status") != 200]
    if failed_before and not still_404:
        answer = ("THE PAGE. Every predicate that 404'd from its own facility "
                  "page returned 200 from this one. The request is fine; the "
                  "session those four pages leave behind is not. Production can "
                  "ask from a page that works.")
    elif failed_before and len(still_404) == len(failed_before):
        answer = ("THE FACILITY. The same bytes 404 from a session that is "
                  "demonstrably healthy. GolfNow's search has nothing indexed "
                  "under these ids — they are listings, not inventory. Retag "
                  "them; there is nothing here to scrape.")
    elif failed_before:
        answer = (f"MIXED — {len(still_404)} of {len(failed_before)} still 404. "
                  "Neither explanation covers it; look at which ones moved.")
    else:
        answer = "nothing to compare — no NO VALID RESPONSE records in the diag."

    lines = [f"golfnow crosscheck — {dt.datetime.now(dt.timezone.utc).isoformat()}",
             f"asked from : {host['url']}",
             f"landed on  : {landed}",
             f"date       : {date_str}",
             "",
             f"ANSWER: {answer}",
             ""]
    for x in rows:
        lines.append(f"  {x['slug']:<40} fid={x['facility_id']:<6} "
                     f"status={x.get('status')} total={x.get('total')} "
                     f"mine={x.get('mine')} ct={x.get('content_type','')[:24]} "
                     f"{('html=' + repr(x.get('title'))) if x.get('not_json') else ''}"
                     f"{(' error=' + x['error']) if x.get('error') else ''}")
        lines.append(f"      was: {x['diag_verdict']}")
    p = pathlib.Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))
    p.with_suffix(".json").write_text(json.dumps(rows, indent=1))
    print(f"\nANSWER: {answer}\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
