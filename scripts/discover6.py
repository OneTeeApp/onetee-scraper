"""Probe #6 — verify the recheck findings.

1. gypsumcreekgolf.cps.golf: GetAllOptions -> pin webSiteId + course ids.
2. eagletrace.cps.golf mobile search page: does the tee sheet render
   anonymously, or is sign-in enforced? Capture API calls + auth mode.
3. Broadlands via the Chronogolf adapter (plain): the Noteefy page is a skin
   over a Chronogolf tee sheet (white-label app com.chronogolf.booking.broadlands)
   — does the marketplace API serve it?
4. goibsvision WebRes (Copper Creek ccgc, Telluride): render in browser,
   count times, capture any XHR (plain clients get 500'd).
5. SuperSaaS Spreading Antlers: render, capture the schedule's data calls.
6. GolfNow resale inventory checks: Broadlands 1413, Walking Stick 2724.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOMORROW = dt.date.today() + dt.timedelta(days=1)

CPS_JS = r"""
async ([tenant]) => {
  const base = "https://" + tenant + ".cps.golf";
  const tr = await fetch(base + "/identityapi/myconnect/token/short",
    {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
     body:"client_id=onlinereswebshortlived"});
  if (tr.status !== 200) return {stage:"token", status:tr.status};
  const token = (await tr.json()).access_token;
  const H = {Authorization:"Bearer "+token, "client-id":"onlineresweb",
    "x-productid":"1","x-componentid":"1","x-siteid":"1","x-moduleid":"7",
    "x-terminalid":"3","Accept":"application/json"};
  const ar = await fetch(base + "/onlineres/onlineapi/api/v1/onlinereservation/GetAllOptions/" + tenant, {headers:H});
  if (ar.status !== 200) return {stage:"getalloptions", status:ar.status};
  const j = await ar.json();
  return {stage:"ok", webSiteId: j.webSiteId || null,
          courseOptions: (j.courseOptions||[]).map(c=>({id:c.courseId??c.id,
            name:c.courseName??c.name}))};
}
"""

GN_JS = r"""
() => {
  const txt = (document.body && document.body.innerText || "");
  return {title: document.title.slice(0,60),
          times: (txt.match(/\d?\d:\d\d\s*[AP]M/gi) || []).length};
}
"""


def render_capture(pw, name, url, interest, wait_ms=12000, extra_wait_selector=None):
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits, reqs = [], {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_req(rq):
            if any(k in rq.url.lower() for k in interest):
                reqs[rq.url] = {"m": rq.method, "post": (rq.post_data or "")[:300]}

        def on_resp(resp):
            u = resp.url.lower()
            if not any(k in u for k in interest):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct and "xml" not in ct:
                return
            try:
                body = resp.text()
            except Exception:
                return
            hits.append({"url": resp.url[:160], "status": resp.status,
                         "req": reqs.get(resp.url), "len": len(body),
                         "body": body[:1000]})

        pg.on("request", on_req)
        pg.on("response", on_resp)
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(wait_ms)
        dom = pg.evaluate("""() => {
          const txt = (document.body && document.body.innerText || "");
          return {title: document.title.slice(0,70),
                  times: (txt.match(/\\d?\\d:\\d\\d\\s*[AP]M/gi)||[]).length,
                  text: txt.replace(/\\s+/g," ").slice(0,350)};
        }""")
        print(f"RESULT {name}: dom={json.dumps(dom)}", flush=True)
        print(f"  finalUrl: {pg.url[:140]}", flush=True)
        for h in hits[:10]:
            print(f"  {h['status']} {h['req']['m'] if h['req'] else '?'} {h['url']} len={h['len']}",
                  flush=True)
            if h["req"] and h["req"]["post"]:
                print(f"    POST: {h['req']['post']}", flush=True)
            print(f"    BODY: {h['body'][:800]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT {name}: ERROR {type(e).__name__} {str(e)[:100]}", flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright

    # 3. Broadlands chronogolf (plain, via the real adapter)
    from scraper.adapters.chronogolf import ChronogolfAdapter
    ad = ChronogolfAdapter()
    fake = {"slug": "broadlands-golf-course", "name": "Broadlands Golf Course",
            "city": "Broomfield", "platform": "chronogolf",
            "booking_url": "https://www.chronogolf.com/club/broadlands-golf-course",
            "ids": {"slug": "broadlands-golf-course", "club_uuid": None}}
    try:
        disc = ad.discover("broadlands-golf-course")
        print(f"RESULT broadlands-chrono discover: {json.dumps(disc)[:400]}", flush=True)
        tts = ad.fetch(fake, TOMORROW)
        print(f"RESULT broadlands-chrono fetch: OK {len(tts)} times "
              f"e.g. {tts[0].to_dict()['teetime'] if tts else None}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT broadlands-chrono: FAIL {type(e).__name__} {str(e)[:140]}",
              flush=True)

    with sync_playwright() as pw:
        # 1. Gypsum pin
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA)
            pg.goto("https://gypsumcreekgolf.cps.golf/onlineresweb/search-teetime",
                    wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(1500)
            r = pg.evaluate(CPS_JS, ["gypsumcreekgolf"])
            print(f"RESULT cps gypsumcreekgolf: {json.dumps(r)[:600]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT cps gypsumcreekgolf: ERROR {type(e).__name__}", flush=True)
        finally:
            b.close()

        # 2. Eagle Trace anonymous check (mobile search page + API capture)
        render_capture(pw, "eagletrace-m",
                       "https://eagletrace.cps.golf/onlineresweb/m/search-teetime/default",
                       ["cps.golf"], wait_ms=10000)

        # 4. goibsvision
        render_capture(pw, "goibs ccgc",
                       "https://www.goibsvision.com/WebRes/Club/ccgc/Browse",
                       ["goibsvision"], wait_ms=12000)
        render_capture(pw, "goibs telluride",
                       "https://www.goibsvision.com/WebRes/Club/Telluride/Browse",
                       ["goibsvision"], wait_ms=12000)

        # 5. SuperSaaS
        render_capture(pw, "supersaas antlers",
                       "https://www.supersaas.com/schedule/Terry%27s_Golf/SAGC_TEE_TIMES",
                       ["supersaas"], wait_ms=10000)

        # 6. GolfNow resale inventory
        for fid, slug in (("1413", "the-broadlands-golf-course"),
                          ("2724", "walking-stick-golf-course")):
            b = pw.chromium.launch(args=["--no-sandbox"])
            try:
                pg = b.new_page(user_agent=UA)
                pg.goto(f"https://www.golfnow.com/tee-times/facility/{fid}-{slug}/search",
                        wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(9000)
                print(f"RESULT golfnow {fid}-{slug}: {json.dumps(pg.evaluate(GN_JS))}",
                      flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"RESULT golfnow {fid}-{slug}: ERROR {type(e).__name__}", flush=True)
            finally:
                b.close()


if __name__ == "__main__":
    main()
