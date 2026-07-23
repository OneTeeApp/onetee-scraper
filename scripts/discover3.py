"""Discovery probe #3 — close the gaps from discover2:

1. CPS GetAllOptions with CORRECT keys (webSiteId / courseOptions) for
   cityofwestminster + marianabutte -> values to pin in EXTRA_IDS.
2. Eagle Trace: load eagletrace.cps.golf, log final URL + API hosts to find
   the real tenant (token endpoint 404'd).
3. Granby Ranch: try kenna alias candidates against /alias/<a>/facilities.
4. Meeker ForeUp 22597: dump booking page config snippet (why no schedule_id).
5. Patty Jewett / Valley Hi: retry ForeUp times with each discovered
   booking_class (0 times might mean class-gated rates).
6. Noteefy Broadlands: 40s wait for the managed challenge, then capture APIs.
7. ForeTees Public_teesheet via PLAIN requests (no browser) — if it works
   from GH plain, the adapter needs no Chromium.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

import requests

sys.path.insert(0, ".")
from scraper.adapters.base import USER_AGENT  # noqa: E402

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
  const co = (j.courseOptions || []).map(c => ({
      id: c.courseId ?? c.id, name: c.courseName ?? c.name ?? c.description}));
  return {stage:"ok", webSiteId: j.webSiteId || null, courseOptions: co,
          sampleCourseOption: (j.courseOptions||[])[0] || null};
}
"""


def browser_part():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        # 1. CPS pinning
        for tenant in ("cityofwestminster", "marianabutte"):
            b = pw.chromium.launch(args=["--no-sandbox"])
            try:
                pg = b.new_page(user_agent=UA)
                pg.goto(f"https://{tenant}.cps.golf/onlineresweb/search-teetime",
                        wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(1500)
                r = pg.evaluate(CPS_JS, [tenant])
                print(f"RESULT cps3 {tenant}: {json.dumps(r)[:1000]}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"RESULT cps3 {tenant}: ERROR {type(e).__name__}", flush=True)
            finally:
                b.close()

        # 2. Eagle Trace diagnosis
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA)
            hosts = set()
            pg.on("request", lambda rq: hosts.add(rq.url.split("/")[2])
                  if "://" in rq.url else None)
            pg.goto("https://eagletrace.cps.golf/", wait_until="domcontentloaded",
                    timeout=40000)
            pg.wait_for_timeout(9000)
            print(f"RESULT eagletrace: final={pg.url} title={pg.title()[:50]!r} "
                  f"hosts={sorted(hosts)[:12]}", flush=True)
            snippet = pg.evaluate(
                "() => (document.body ? document.body.innerText : '').replace(/\\s+/g,' ').slice(0,300)")
            print(f"  body: {snippet!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT eagletrace: ERROR {type(e).__name__} {str(e)[:80]}",
                  flush=True)
        finally:
            b.close()

        # 6. Noteefy with long wait
        b = pw.chromium.launch(args=["--no-sandbox"])
        hits, reqs = [], {}
        try:
            pg = b.new_page(user_agent=UA)

            def on_req(rq):
                if "noteefy" in rq.url and rq.method in ("POST", "GET"):
                    reqs[rq.url] = {"m": rq.method, "post": (rq.post_data or "")[:400]}

            def on_resp(resp):
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct or "noteefy" not in resp.url:
                    return
                try:
                    body = json.dumps(resp.json())
                except Exception:
                    return
                hits.append({"url": resp.url[:170], "status": resp.status,
                             "req": reqs.get(resp.url), "len": len(body),
                             "body": body[:1400]})

            pg.on("request", on_req)
            pg.on("response", on_resp)
            pg.goto("https://booking.noteefy.app/e/ba16828e-77e6-425f-b63d-ac27e785f69d",
                    wait_until="domcontentloaded", timeout=45000)
            for i in range(8):          # up to 40s for the managed challenge
                pg.wait_for_timeout(5000)
                t = pg.title()
                if "moment" not in t.lower():
                    break
            print(f"RESULT noteefy2: title={pg.title()[:60]!r} json={len(hits)}",
                  flush=True)
            for h in hits[:12]:
                print(f"  {h['status']} {h['req']['m'] if h['req'] else '?'} {h['url']}",
                      flush=True)
                if h["req"] and h["req"]["post"]:
                    print(f"    POST: {h['req']['post']}", flush=True)
                print(f"    BODY[{h['len']}]: {h['body']}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT noteefy2: ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
        finally:
            b.close()


def plain_part():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    # 3. Granby kenna alias candidates
    for alias in ("golf-granby-ranch", "granby-ranch", "golfgranbyranch",
                  "granby-ranch-golf-course", "golf-granby-ranch-co"):
        try:
            r = s.get(f"https://phx-api-be-east-1b.kenna.io/alias/{alias}/facilities",
                      headers={"x-be-alias": alias}, timeout=20)
            ok = r.status_code
            names = None
            if ok == 200:
                try:
                    names = [f.get("name") for f in r.json()][:4]
                except Exception:
                    names = "parse-fail"
            print(f"RESULT granby alias {alias}: {ok} {names}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT granby alias {alias}: FAIL {type(e).__name__}", flush=True)

    # 4. Meeker booking page dump
    try:
        r = s.get("https://foreupsoftware.com/index.php/booking/22597", timeout=25)
        text = r.text
        m = re.findall(r'"schedule_id"\s*:\s*"?\d+|schedules\s*:\s*\[[^\]]{0,200}|bookingClasses[^\]]{0,150}', text)
        title = re.search(r"<title>(.*?)</title>", text, re.S)
        print(f"RESULT meeker page: {r.status_code} len={len(text)} "
              f"title={title.group(1).strip()[:60] if title else None!r} "
              f"matches={m[:6]}", flush=True)
        # also try the newer booking URL form
        r2 = s.get("https://foreupsoftware.com/index.php/booking/22597/22597", timeout=25)
        print(f"RESULT meeker alt: {r2.status_code} len={len(r2.text)}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT meeker page: FAIL {type(e).__name__}", flush=True)

    # 5. Patty Jewett / Valley Hi booking_class retry
    for slug, course_id, sched, classes in (
        ("patty-jewett", "19459", "1670", ["1339", "51076", "5535", "7496", "8427"]),
        ("valley-hi", "19457", "1668", ["4502", "7475", "8429"]),
    ):
        for bc in [None] + classes:
            try:
                params = {
                    "time": "all", "date": TOMORROW.strftime("%m-%d-%Y"),
                    "holes": "all", "players": "0",
                    "schedule_id": sched, "schedule_ids[]": sched,
                    "specials_only": "0", "api_key": "no_limits",
                }
                if bc:
                    params["booking_class"] = bc
                r = s.get("https://foreupsoftware.com/index.php/api/booking/times",
                          params=params, timeout=25,
                          headers={"Referer": f"https://foreupsoftware.com/index.php/booking/{course_id}"})
                n = len(r.json()) if r.status_code == 200 else None
                print(f"RESULT foreup-bc {slug} bc={bc}: {r.status_code} times={n}",
                      flush=True)
                if n:
                    break
            except Exception as e:  # noqa: BLE001
                print(f"RESULT foreup-bc {slug} bc={bc}: FAIL {type(e).__name__}",
                      flush=True)

    # 7. ForeTees plain HTTP
    try:
        d = TOMORROW.isoformat()
        r = s.get("https://web.foretees.com/v5/servlet/Public_teesheet",
                  params={"cid": "1087", "ckey": "gnES75QmoZRMwPaW",
                          "a": "vts", "d": d}, timeout=25)
        n = None
        if r.status_code == 200:
            data = r.json()["foreTeesPublicTimesApiResp"]["data"]
            n = len(data[0].get("publicTimes", [])) if data else 0
        print(f"RESULT foretees plain: {r.status_code} times={n}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT foretees plain: FAIL {type(e).__name__} {str(e)[:80]}", flush=True)


def main():
    plain_part()
    browser_part()


if __name__ == "__main__":
    main()
