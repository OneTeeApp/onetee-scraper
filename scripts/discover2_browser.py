"""Discovery probe #2 (browser): pin IDs / API shapes for newly-found booking
systems. Same legitimacy rules as always — real Chromium, real public pages,
no stealth, no challenge-solving.

1. CPS tenants (cityofwestminster NEW, eagletrace REVIVED, cityofloveland
   umbrella, marianabutte retry): anonymous token + GetAllOptions in-page to
   capture websiteId + course ids for EXTRA_IDS pinning.
2. EZLinks portal heritageeaglebendnrpp: init + search in-page (does the
   managed challenge clear? how many courses/slots?).
3. Quick18 heritagenonresident: plain page load, count tee rows.
4. Noteefy (Broadlands): load booking page, capture ALL API request/response
   pairs so we can build an adapter.
5. ForeTees public portal (Dalton Ranch): same network capture.

Prints RESULT lines; workflow commits them to probe-results/.
"""
from __future__ import annotations

import datetime as dt
import json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CPS_TENANTS = ["cityofwestminster", "eagletrace", "cityofloveland", "marianabutte"]

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
  let j = {}; try { j = await ar.json(); } catch(e) { return {stage:"parse", status:200}; }
  const wid = j.websiteId || (j.onlineWebSiteSetting && j.onlineWebSiteSetting.websiteId) || null;
  const courses = (j.onlineCourses || j.courses || []).map(c => ({id:c.courseId ?? c.id, name:c.courseName ?? c.name}));
  return {stage:"ok", status:200, websiteId:wid, courses,
          topKeys:Object.keys(j).slice(0,20)};
}
"""

EZ_JS = r"""
async ([dateMdy]) => {
  const base = location.origin;
  let init;
  try { init = await (await fetch(base + "/api/search/init")).json(); }
  catch (e) { return {stage:"init", error:String(e).slice(0,60)}; }
  const ids = String(init.AllCourseIDs || "").split(",").filter(x=>x.trim()).map(Number);
  const names = (init.Courses||[]).map(c=>c.CourseName);
  if (!ids.length) return {stage:"init", error:"no ids (challenged?)"};
  const body = {p01:ids,p02:dateMdy,p03:"5:00 AM",p04:"7:00 PM",p05:0,p06:2,p07:false};
  const s = await fetch(base + "/api/search/search",{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  let n=null; try { n=((await s.json()).r06||[]).length; } catch(e){}
  return {stage:"search", status:s.status, count:n, ids, names};
}
"""


def cps(pw, tenant):
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        for attempt in range(3):
            try:
                pg.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                        wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(1200)
                r = pg.evaluate(CPS_JS, [tenant])
                if r.get("status") == 200 or attempt == 2:
                    return r
            except Exception as e:  # noqa: BLE001
                r = {"error": type(e).__name__}
            pg.wait_for_timeout(3000 * (attempt + 1))
        return r
    finally:
        b.close()


def ezlinks(pw, portal, date_mdy):
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://{portal}.ezlinksgolf.com/index.html#!/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(7000)
        return pg.evaluate(EZ_JS, [date_mdy])
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__} {str(e)[:80]}"}
    finally:
        b.close()


def quick18(pw, sub):
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://{sub}.quick18.com/teetimes", wait_until="domcontentloaded",
                timeout=45000)
        pg.wait_for_timeout(5000)
        return pg.evaluate("""() => {
          const txt = document.body ? document.body.innerText : "";
          return {title: document.title.slice(0,60),
                  times: (txt.match(/\\d?\\d:\\d\\d\\s*[AP]M/gi)||[]).length,
                  snippet: txt.replace(/\\s+/g," ").slice(0,200)};
        }""")
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}"}
    finally:
        b.close()


def capture(pw, name, url, interest, wait_ms=15000):
    """Load a JS SPA and dump JSON API request/response pairs."""
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits, reqs = [], {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_request(req):
            u = req.url
            if any(k in u.lower() for k in interest) and "static" not in u:
                try:
                    reqs[u] = {"m": req.method, "post": (req.post_data or "")[:500]}
                except Exception:
                    pass

        def on_response(resp):
            u = resp.url
            if not any(k in u.lower() for k in interest):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            try:
                data = resp.json()
            except Exception:
                return
            body = json.dumps(data)
            hits.append({"url": u[:180], "status": resp.status,
                         "req": reqs.get(u), "len": len(body),
                         "body": body[:1500]})

        pg.on("request", on_request)
        pg.on("response", on_response)
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(wait_ms)
        print(f"RESULT {name}: {len(hits)} json responses, title={pg.title()[:50]!r}",
              flush=True)
        for h in hits[:14]:
            print(f"  {h['status']} {h['req']['m'] if h['req'] else 'GET?'} {h['url']}",
                  flush=True)
            if h["req"] and h["req"]["post"]:
                print(f"    POST: {h['req']['post']}", flush=True)
            print(f"    BODY[{h['len']}]: {h['body']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT {name}: ERROR {type(e).__name__} {str(e)[:100]}", flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    date_mdy = tomorrow.strftime("%m/%d/%Y")
    with sync_playwright() as pw:
        for t in CPS_TENANTS:
            print(f"RESULT cps {t}: {json.dumps(cps(pw, t))[:900]}", flush=True)
        print(f"RESULT ezlinks heritageeaglebendnrpp: "
              f"{json.dumps(ezlinks(pw, 'heritageeaglebendnrpp', date_mdy))[:600]}",
              flush=True)
        print(f"RESULT quick18 heritagenonresident: "
              f"{json.dumps(quick18(pw, 'heritagenonresident'))}", flush=True)
        capture(pw, "noteefy broadlands",
                "https://booking.noteefy.app/e/ba16828e-77e6-425f-b63d-ac27e785f69d",
                ["noteefy", "api"], wait_ms=15000)
        capture(pw, "foretees dalton",
                "https://web.foretees.com/v5/assets/foreteespublic/index.html#/availability?clubKey=gnES75QmoZRMwPaW&cid=1087",
                ["foretees", "api", "availability"], wait_ms=15000)


if __name__ == "__main__":
    main()
