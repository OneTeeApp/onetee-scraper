"""Diagnostic: do EZLinks / GolfNow load via a REAL headless browser from
GitHub's datacenter IP, where the plain HTTP client is blocked?

This ONLY navigates real pages and reads what a normal browser naturally gets —
no stealth plugins, no challenge-solving. If a site's managed challenge auto-
passes for a real browser engine, we get data (legit, same as cps.golf did). If
it stops at an interactive "verify you are human" wall, the probe reports that
and we go no further (defeating it is off-limits).

Read the RESULT lines in the CI log.
"""
from __future__ import annotations

import datetime as dt
import json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EZ_PORTALS = ["cityofaurora", "pinecreekpp", "grandelk"]
GOLFNOW_FACILITIES = ["453-arrowhead-golf-club", "9351-cedaredge-golf-club"]

# EZLinks: run the known init + search flow inside the page (real browser TLS).
EZ_JS = r"""
async ([dateMdy]) => {
  const base = location.origin;
  let init;
  try { init = await (await fetch(base + "/api/search/init")).json(); }
  catch (e) { return {stage:"init", error:String(e).slice(0,60)}; }
  const ids = String(init.AllCourseIDs || "").split(",").filter(x => x.trim()).map(Number);
  if (!ids.length) return {stage:"init", error:"no course ids (challenged?)"};
  const body = {p01:ids, p02:dateMdy, p03:"5:00 AM", p04:"7:00 PM", p05:0, p06:2, p07:false};
  const s = await fetch(base + "/api/search/search", {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  let n = null; try { n = ((await s.json()).r06 || []).length; } catch (e) {}
  return {stage:"search", status:s.status, count:n};
}
"""

GN_JS = r"""
() => {
  const txt = (document.body && document.body.innerText || "");
  const times = (txt.match(/\d?\d:\d\d\s*[AP]M/gi) || []).length;
  const apiHosts = [...new Set(performance.getEntriesByType('resource')
      .map(e => { try { return new URL(e.name).host; } catch (_) { return ""; } }))]
      .filter(h => /api|kenna|golfnow|teeitup/i.test(h));
  const challenge = /just a moment|verify you are human|attention required|cf-/i.test(txt);
  return {times, apiHosts: apiHosts.slice(0, 8), challenge};
}
"""


def ez(pw, portal: str, date_mdy: str) -> dict:
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://{portal}.ezlinksgolf.com/index.html#!/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(7000)  # allow a managed challenge to auto-clear
        title = (pg.title() or "")[:30]
        r = pg.evaluate(EZ_JS, [date_mdy])
        return {"portal": portal, "title": title, **r}
    except Exception as e:  # noqa: BLE001
        return {"portal": portal, "error": f"{type(e).__name__}"}
    finally:
        b.close()


def golfnow(pw, facility: str) -> dict:
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://www.golfnow.com/tee-times/facility/{facility}/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(9000)
        title = (pg.title() or "")[:30]
        info = pg.evaluate(GN_JS)
        return {"facility": facility, "title": title, **info}
    except Exception as e:  # noqa: BLE001
        return {"facility": facility, "error": f"{type(e).__name__}"}
    finally:
        b.close()


def main() -> None:
    from playwright.sync_api import sync_playwright
    date_mdy = (dt.date.today() + dt.timedelta(days=1)).strftime("%m/%d/%Y")
    with sync_playwright() as pw:
        for p in EZ_PORTALS:
            print(f"RESULT ezlinks {p}: {json.dumps(ez(pw, p, date_mdy))}", flush=True)
        for f in GOLFNOW_FACILITIES:
            print(f"RESULT golfnow {f}: {json.dumps(golfnow(pw, f))}", flush=True)


if __name__ == "__main__":
    main()
