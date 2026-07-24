"""Probe v2: find Club Caddie's real tee-time data endpoint + CourseId.

v1 loaded the public view page fine (course name/address render, no CAPTCHA)
but caught no "/slots" call in 8s — the endpoint is named differently or fires
on interaction. This captures ALL network requests and searches the page's
own scripts/HTML for a course id, then replays likely endpoints in-page.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOMORROW = dt.date.today() + dt.timedelta(days=1)

# In-page: dig the CourseId out of window/config/HTML, then try candidate
# tee-time endpoints and report which returns JSON.
DIG_JS = r"""
async ([base, token, dateStr]) => {
  // 1. hunt for a course id in globals, inline JSON, and the DOM
  let cid = null;
  const html = document.documentElement.innerHTML;
  const pats = [/CourseId["'=:\s]+(\d+)/i, /course_id["'=:\s]+(\d+)/i,
                /"courseId"\s*:\s*(\d+)/i, /clubid["'=:\s]+(\d+)/i,
                /data-course-?id="(\d+)"/i];
  for (const p of pats) { const m = html.match(p); if (m) { cid = m[1]; break; } }
  // check a few likely globals
  for (const k of ["courseId","CourseId","clubId","__CONFIG__","config"]) {
    try { const v = window[k]; if (typeof v === "number") { cid = String(v); break; }
      if (v && (v.courseId || v.CourseId)) { cid = String(v.courseId||v.CourseId); break; } }
    catch (e) {}
  }
  // 2. candidate endpoints (GET) — report status + whether JSON
  const cands = [
    `/webapi/view/${token}/slots?date=${dateStr}&player=1&holes=any&fromtime=5&totime=22&minprice=0&maxprice=9999&ratetype=any&HoleGroup=any&CourseId=${cid}&apikey=${token}`,
    `/webapi/view/${token}/teetimes?date=${dateStr}&CourseId=${cid}&apikey=${token}`,
    `/webapi/view/${token}/availability?date=${dateStr}&CourseId=${cid}&apikey=${token}`,
    `/webapi/teetimes/${token}?date=${dateStr}`,
    `/webapi/view/${token}/GetTeeTimes?date=${dateStr}&CourseId=${cid}`,
  ];
  const out = {cid, tries: []};
  for (const path of cands) {
    try {
      const r = await fetch(base + path, {headers:{"X-Requested-With":"XMLHttpRequest",
        "Accept":"application/json, text/javascript, */*; q=0.01"}});
      const txt = await r.text();
      const isJson = txt.trim()[0] === "{" || txt.trim()[0] === "[";
      let count = null, keys = null, sample = null;
      if (isJson) { try { const j = JSON.parse(txt);
        const list = Array.isArray(j) ? j : (j.slots||j.Slots||j.data||j.teeTimes||j.TeeTimes||j.times||j.Result||[]);
        count = Array.isArray(list) ? list.length : null;
        if (count) { keys = Object.keys(list[0]); sample = list[0]; } } catch(e){} }
      out.tries.push({path: path.slice(0,60), status: r.status, isJson, count,
                      keys, sample, head: isJson ? null : txt.slice(0,80)});
    } catch (e) { out.tries.push({path: path.slice(0,60), error: String(e).slice(0,50)}); }
  }
  return out;
}
"""


def probe(pw, course):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    net = []
    try:
        pg = b.new_page(user_agent=UA)
        pg.on("request", lambda rq: net.append((rq.method, rq.url))
              if any(k in rq.url for k in ("webapi", "api", "slot", "teetime",
                                           "availab", "TeeTime")) else None)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="networkidle", timeout=40000)
        pg.wait_for_timeout(6000)
        res = pg.evaluate(DIG_JS, [base, token, TOMORROW.strftime("%m/%d/%Y")])
        print(f"RESULT cc {course['slug']}: cid={res.get('cid')}", flush=True)
        print("  network calls:", flush=True)
        for m, u in net[:14]:
            # trim host, keep path+query
            short = u.split(".clubcaddie.com", 1)[-1][:120]
            print(f"    {m} {short}", flush=True)
        for t in res.get("tries", []):
            if t.get("count"):
                print(f"  HIT {t['path']} -> {t['count']} slots keys={t['keys']}",
                      flush=True)
                print(f"    SAMPLE: {json.dumps(t['sample'])[:500]}", flush=True)
            else:
                print(f"  miss {t.get('path')} status={t.get('status')} "
                      f"json={t.get('isJson')} {t.get('head') or t.get('error') or ''}",
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
    # probe just 3 representative courses to find the endpoint pattern
    sample = [c for c in ccs if c["slug"] in
              ("applewood-golf-course", "salida-golf-club", "eaglevail-golf-club")]
    with sync_playwright() as pw:
        for c in (sample or ccs[:3]):
            probe(pw, c)


if __name__ == "__main__":
    main()
