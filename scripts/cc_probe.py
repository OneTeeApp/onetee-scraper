"""Probe v7: on the Club Caddie view page (auto-renders today's slots), dump
(a) the first tee-time cards' HTML for parser design, and (b) the date input +
Search control selectors so the fetcher can drive future dates.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def probe(pw, course):
    ids = course["ids"]
    base = f"https://apimanager-{ids['shard']}.clubcaddie.com"
    token = ids["view_token"]
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded",
                timeout=40000)
        pg.wait_for_timeout(7000)
        info = pg.evaluate(r"""() => {
          const out = {};
          const all = [...document.querySelectorAll("*")];
          // tee-time cards: leaf element whose text is exactly a time
          const timeEls = all.filter(e => e.children.length === 0 &&
              /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent||""));
          out.timeCount = timeEls.length;
          // climb to a card container that also holds a price
          const cards = [];
          for (const te of timeEls.slice(0, 3)) {
            let card = te;
            for (let i=0;i<6 && card.parentElement;i++){
              card = card.parentElement;
              if (/\$/.test(card.textContent)) break;
            }
            cards.push(card.outerHTML.replace(/\s+/g," ").slice(0, 700));
          }
          out.cards = cards;
          // date input candidates
          const dateInputs = all.filter(e => e.tagName === "INPUT" &&
              /date/i.test((e.id||"")+(e.name||"")+(e.className||"")+(e.placeholder||"")));
          out.dateInputs = dateInputs.slice(0,4).map(e => ({
            id:e.id, name:e.name, cls:e.className, ph:e.placeholder, val:e.value}));
          // search button candidates
          const btns = all.filter(e => /button|^a$/i.test(e.tagName) &&
              /search/i.test(e.textContent||"") && e.textContent.length < 30);
          out.searchBtns = btns.slice(0,4).map(e => ({
            tag:e.tagName, id:e.id, cls:e.className, txt:(e.textContent||"").trim().slice(0,20)}));
          return out;
        }""")
        print(f"RESULT cc {course['slug']}: times={info.get('timeCount')}", flush=True)
        print(f"  dateInputs={json.dumps(info.get('dateInputs'))}", flush=True)
        print(f"  searchBtns={json.dumps(info.get('searchBtns'))}", flush=True)
        for i, c in enumerate(info.get("cards") or []):
            print(f"  CARD{i}: {c}", flush=True)
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
