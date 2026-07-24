"""Probe v6: Club Caddie /slots is a server-rendered HTML page with the tee
times in the DOM. Establish session (view page) -> navigate to /slots for the
target date -> dump ordered card text + one card's HTML to design the parser.
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
MDY = TOMORROW.strftime("%m/%d/%Y")


def probe(pw, course):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()
        # 1. establish session
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded",
                timeout=40000)
        pg.wait_for_timeout(4000)
        # 2. navigate to the slots page for tomorrow
        pg.goto(f"{base}/webapi/view/{token}/slots?date={MDY}&player=1&ratetype=any",
                wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(4000)
        info = pg.evaluate(r"""() => {
          const out = {url: location.href.slice(0,120)};
          const txt = document.body ? document.body.innerText : "";
          out.lines = txt.split("\n").map(s=>s.trim()).filter(Boolean).slice(0, 60);
          // find the element wrapping the first time, dump its card HTML
          const all = [...document.querySelectorAll("*")];
          const timeEl = all.find(e => /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent||"")
                                    && e.children.length === 0);
          if (timeEl) {
            let card = timeEl;
            for (let i=0;i<5 && card.parentElement;i++){
              card = card.parentElement;
              if (card.textContent.match(/\$|\bholes?\b|player/i)) break;
            }
            out.cardHTML = card.outerHTML.replace(/\s+/g," ").slice(0, 900);
            out.cardClass = card.className;
          }
          return out;
        }""")
        print(f"RESULT cc {course['slug']}: url={info.get('url')}", flush=True)
        print("  LINES: " + json.dumps(info.get("lines"))[:900], flush=True)
        print(f"  cardClass={info.get('cardClass')!r}", flush=True)
        print(f"  cardHTML={info.get('cardHTML')}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT cc {course['slug']}: ERROR {type(e).__name__} {str(e)[:90]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    ccs = [c for c in reg if c["platform"] == "clubcaddie" and c["ids"].get("shard")]
    want = [c for c in ccs if c["slug"] == "applewood-golf-course"]
    with sync_playwright() as pw:
        for c in want:
            probe(pw, c)


if __name__ == "__main__":
    main()
