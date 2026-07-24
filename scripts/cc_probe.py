"""Probe v4: observe Club Caddie's own /slots responses + rendered DOM.

Goal: decide between (a) replicating the page's own /slots JSON call exactly,
or (b) scraping the rendered tee-time cards from the DOM. Captures every
/slots response (status, content-type, head), whether the widget accepts a
date via the view URL, and the tee-time text the page renders.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOMORROW = dt.date.today() + dt.timedelta(days=1)
MDY = TOMORROW.strftime("%m/%d/%Y")


def probe(pw, course, try_date_url):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    slots_resps = []
    try:
        pg = b.new_page(user_agent=UA)

        def on_resp(resp):
            if "/slots" in resp.url:
                ct = (resp.headers.get("content-type") or "")[:30]
                try:
                    head = resp.text()[:120]
                except Exception:
                    head = "<unreadable>"
                slots_resps.append({
                    "date": parse_qs(urlparse(resp.url).query).get("date", [None])[0],
                    "status": resp.status, "ct": ct,
                    "json": head.strip()[:1] in "{[", "head": head})

        pg.on("response", on_resp)
        url = f"{base}/webapi/view/{token}"
        if try_date_url:
            url += f"?date={MDY}"
        pg.goto(url, wait_until="networkidle", timeout=40000)
        pg.wait_for_timeout(6000)
        dom = pg.evaluate(r"""() => {
          const txt = document.body ? document.body.innerText : "";
          // tee-time cards usually show a time + price; grab lines with a time
          const lines = txt.split("\n").map(s=>s.trim()).filter(Boolean);
          const timeLines = lines.filter(l => /\d?\d:\d\d\s*[AP]M/i.test(l));
          return {totalLines: lines.length, timeCount: timeLines.length,
                  samples: timeLines.slice(0, 6),
                  hasDatePicker: !!document.querySelector('input[type=\"text\"],.datepicker,[class*=date]')};
        }""")
        print(f"RESULT cc {course['slug']} (dateUrl={try_date_url}):", flush=True)
        for s in slots_resps:
            print(f"  /slots date={s['date']} status={s['status']} ct={s['ct']} "
                  f"json={s['json']} head={s['head'][:70]!r}", flush=True)
        print(f"  DOM: {json.dumps(dom)[:400]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT cc {course['slug']}: ERROR {type(e).__name__} {str(e)[:80]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    ccs = [c for c in reg if c["platform"] == "clubcaddie" and c["ids"].get("shard")]
    apple = [c for c in ccs if c["slug"] == "applewood-golf-course"]
    with sync_playwright() as pw:
        for c in apple:
            probe(pw, c, try_date_url=False)   # default date
            probe(pw, c, try_date_url=True)    # date via view URL
        # one more course for cross-check
        for c in [x for x in ccs if x["slug"] == "eaglevail-golf-club"]:
            probe(pw, c, try_date_url=True)


if __name__ == "__main__":
    main()
