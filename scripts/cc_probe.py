"""Probe: how does the Club Caddie widget load a FUTURE date's cards?
Try strategies, report time counts + final URL for each."""
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

COUNT_JS = r"""() => {
  const all=[...document.querySelectorAll("*")];
  const t=all.filter(e=>e.children.length===0 && /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent||""));
  return {times:t.length, url:location.href.slice(0,110),
          sample:t.slice(0,4).map(e=>e.textContent.trim())};
}"""


def strat(pw, base, token, name, fn):
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()
        fn(pg)
        r = pg.evaluate(COUNT_JS)
        print(f"  [{name}] {json.dumps(r)}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [{name}] ERROR {type(e).__name__} {str(e)[:70]}", flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    c = [x for x in reg if x["slug"] == "salida-golf-club"][0]
    base = f"https://apimanager-{c['ids']['shard']}.clubcaddie.com"
    token = c["ids"]["view_token"]
    print(f"RESULT strategies for salida (date {MDY}):", flush=True)

    def A(pg):  # view?date=
        pg.goto(f"{base}/webapi/view/{token}?date={MDY}", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(9000)

    def B(pg):  # view, then fill date + click search, wait long
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(4000)
        pg.fill("#dateinput", MDY)
        pg.click("#UpdateFilerButton")
        pg.wait_for_timeout(11000)

    def C(pg):  # view default (today baseline), long wait, no interaction
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(11000)

    def D(pg):  # slots page with explicit date, long wait (maybe auto-runs)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(3000)
        pg.goto(f"{base}/webapi/view/{token}/slots?date={MDY}&player=1&ratetype=any",
                wait_until="networkidle", timeout=40000)
        pg.wait_for_timeout(9000)

    with sync_playwright() as pw:
        for name, fn in (("A view?date", A), ("B fill+search", B),
                         ("C today-baseline", C), ("D slots-nav", D)):
            strat(pw, base, token, name, fn)


if __name__ == "__main__":
    main()
