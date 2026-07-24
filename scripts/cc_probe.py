"""Probe: Club Caddie via a real headless browser.

Plain HTTP gets Club Caddie's HTML shell (session-gated). A real Chromium loads
the public view page, its own JS establishes the session and calls the /slots
endpoint — we capture that request (CourseId + params) and its JSON response
(slot shape) for every course, then in-page re-fetch tomorrow to confirm data.
No stealth, no challenge-solving — just the page's own public XHR.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOMORROW = (dt.date.today() + dt.timedelta(days=1))

# in-page: replay the page's own slots call for a target date, all hole groups
REPLAY_JS = r"""
async ([base, token, courseId, dateStr]) => {
  const out = [];
  for (const hg of ["front", "back", "any"]) {
    const qs = new URLSearchParams({date: dateStr, player: "1", holes: "any",
      fromtime: "5", totime: "22", minprice: "0", maxprice: "9999",
      ratetype: "any", HoleGroup: hg, CourseId: courseId, apikey: token});
    let r, j;
    try {
      r = await fetch(base + "/webapi/view/" + token + "/slots?" + qs,
        {headers: {"X-Requested-With": "XMLHttpRequest",
                   "Accept": "application/json, text/javascript, */*; q=0.01"}});
      const txt = await r.text();
      if (txt.trim()[0] === "<") { out.push({hg, status: r.status, html: true}); continue; }
      j = JSON.parse(txt);
    } catch (e) { out.push({hg, error: String(e).slice(0, 60)}); continue; }
    const list = Array.isArray(j) ? j
      : (j.slots || j.Slots || j.data || j.teeTimes || j.TeeTimes || j.times || []);
    out.push({hg, status: r.status, count: list.length,
              sample: list[0] || null, keys: list[0] ? Object.keys(list[0]) : null});
    if (hg === "any" && list.length) break;
  }
  return out;
}
"""


def probe(pw, course):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    if not (shard and token):
        print(f"RESULT cc {course['slug']}: no shard/token ids={ids}", flush=True)
        return
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    seen = {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_req(rq):
            if "/slots" in rq.url:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(rq.url).query)
                seen["courseId"] = (q.get("CourseId") or [None])[0]
                seen["url"] = rq.url[:150]

        pg.on("request", on_req)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded",
                timeout=40000)
        pg.wait_for_timeout(8000)   # let the page fire its own slots call
        cid = seen.get("courseId") or ids.get("clubcaddie_course_id")
        title = (pg.title() or "")[:40]
        if not cid:
            txt = pg.evaluate("() => (document.body?document.body.innerText:'').slice(0,120)")
            print(f"RESULT cc {course['slug']}: no CourseId captured "
                  f"title={title!r} body={txt!r}", flush=True)
            return
        res = pg.evaluate(REPLAY_JS, [base, token, str(cid),
                                      TOMORROW.strftime("%m/%d/%Y")])
        print(f"RESULT cc {course['slug']}: CourseId={cid} title={title!r}",
              flush=True)
        for r in res:
            if r.get("count"):
                print(f"  {r['hg']}: {r['count']} slots keys={r.get('keys')}",
                      flush=True)
                print(f"    SAMPLE: {json.dumps(r.get('sample'))[:600]}", flush=True)
            else:
                print(f"  {r['hg']}: {json.dumps({k: v for k, v in r.items() if k != 'sample'})[:160]}",
                      flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT cc {course['slug']}: ERROR {type(e).__name__} {str(e)[:80]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    ccs = [c for c in reg if c["platform"] == "clubcaddie" and c["ids"].get("shard")]
    with sync_playwright() as pw:
        for c in ccs:
            probe(pw, c)


if __name__ == "__main__":
    main()
