"""Probe v3: Club Caddie — capture the Interaction session token + the page's
own /slots response shape, then replay /slots for tomorrow with that token.

v2 revealed the real call: /webapi/view/<token>/slots?date=..&player=1&
ratetype=any&Interaction=<sessionId>  (no CourseId). Without Interaction the
endpoint returns a "PHPSESSID expired" HTML stub; with it, JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOMORROW = dt.date.today() + dt.timedelta(days=1)

REPLAY_JS = r"""
async ([base, token, interaction, dateStr]) => {
  const url = base + `/webapi/view/${token}/slots?date=${encodeURIComponent(dateStr)}`
    + `&player=1&ratetype=any&Interaction=${interaction}`;
  const r = await fetch(url, {headers:{"X-Requested-With":"XMLHttpRequest",
    "Accept":"application/json, text/javascript, */*; q=0.01"}});
  const txt = await r.text();
  if (txt.trim()[0] !== "{" && txt.trim()[0] !== "[")
    return {status:r.status, html:true, head:txt.slice(0,80)};
  const j = JSON.parse(txt);
  const list = Array.isArray(j) ? j
    : (j.slots||j.Slots||j.data||j.teeTimes||j.TeeTimes||j.times||j.Result||j.result||[]);
  return {status:r.status, topKeys:Array.isArray(j)?null:Object.keys(j),
          count:Array.isArray(list)?list.length:null,
          keys:list[0]?Object.keys(list[0]):null, sample:list[0]||null};
}
"""


def probe(pw, course):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    interaction = {}
    own_slots = {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_req(rq):
            q = parse_qs(urlparse(rq.url).query)
            if q.get("Interaction"):
                interaction["id"] = q["Interaction"][0]

        def on_resp(resp):
            if "/slots" in resp.url and "Interaction" in resp.url:
                try:
                    t = resp.text()
                    if t.strip()[:1] in "{[":
                        own_slots["body"] = t[:1200]
                        own_slots["date"] = (parse_qs(urlparse(resp.url).query)
                                             .get("date", [None])[0])
                except Exception:
                    pass

        pg.on("request", on_req)
        pg.on("response", on_resp)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="networkidle", timeout=40000)
        pg.wait_for_timeout(5000)
        iid = interaction.get("id")
        print(f"RESULT cc {course['slug']}: interaction={iid} "
              f"ownSlotsDate={own_slots.get('date')}", flush=True)
        if own_slots.get("body"):
            print(f"  OWN /slots body: {own_slots['body'][:500]}", flush=True)
        if iid:
            r = pg.evaluate(REPLAY_JS, [base, token, iid,
                                        TOMORROW.strftime("%m/%d/%Y")])
            print(f"  REPLAY tomorrow: {json.dumps(r)[:700]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT cc {course['slug']}: ERROR {type(e).__name__} {str(e)[:80]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    ccs = [c for c in reg if c["platform"] == "clubcaddie" and c["ids"].get("shard")]
    sample = [c for c in ccs if c["slug"] in
              ("applewood-golf-course", "salida-golf-club", "eaglevail-golf-club")]
    with sync_playwright() as pw:
        for c in (sample or ccs[:3]):
            probe(pw, c)


if __name__ == "__main__":
    main()
