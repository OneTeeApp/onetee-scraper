"""Probe: drive the Club Caddie datepicker like a user.

The view page renders today's tee sheet in-place; clicking Search navigates
away and loses state. A real user opens the material datepicker and clicks a
day, which fires the SPA's own onChange -> in-place results. This probe:
  1. loads view, confirms today's baseline time count
  2. opens the datepicker (click #dateinput), dumps the calendar DOM
  3. clicks tomorrow's day cell, then Search, polls up to 20s for times
  4. if still 0, dumps the results-container HTML to see what's there
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

COUNT_JS = r"""() => {
  const all=[...document.querySelectorAll("*")];
  const t=all.filter(e=>e.children.length===0 && /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent||""));
  return t.length;
}"""

CAL_JS = r"""() => {
  // find any visible calendar/picker overlay
  const cands = [...document.querySelectorAll(
    ".dtp, .datepicker, .bootstrap-datetimepicker-widget, [class*=picker], [class*=calendar]")]
    .filter(e => e.offsetParent !== null);
  const dump = cands.slice(0,2).map(e => ({
    cls: e.className,
    html: e.outerHTML.replace(/\s+/g," ").slice(0, 700)}));
  // clickable day cells
  const days = [...document.querySelectorAll("td, .day, [class*=day]")]
    .filter(e => e.offsetParent !== null && /^\d{1,2}$/.test((e.textContent||"").trim()));
  return {overlays: dump, dayCells: days.slice(0,40).map(e => ({
    t:(e.textContent||"").trim(), cls:e.className, tag:e.tagName}))};
}"""


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    c = [x for x in reg if x["slug"] == "salida-golf-club"][0]
    base = f"https://apimanager-{c['ids']['shard']}.clubcaddie.com"
    token = c["ids"]["view_token"]
    day = str(TOMORROW.day)
    print(f"RESULT cc-cal salida (target {TOMORROW.isoformat()}, day {day}):", flush=True)

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = b.new_context(user_agent=UA)
            pg = ctx.new_page()
            pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded",
                    timeout=40000)
            pg.wait_for_timeout(8000)
            print(f"  baseline today times: {pg.evaluate(COUNT_JS)} url={pg.url[:90]}",
                  flush=True)

            # open the datepicker
            try:
                pg.click("#dateinput", timeout=6000)
                pg.wait_for_timeout(1500)
            except Exception as e:  # noqa: BLE001
                print(f"  click #dateinput failed: {type(e).__name__}", flush=True)
            cal = pg.evaluate(CAL_JS)
            print(f"  overlays: {json.dumps(cal['overlays'])[:800]}", flush=True)
            print(f"  dayCells sample: {json.dumps(cal['dayCells'][:20])[:500]}",
                  flush=True)

            # try clicking tomorrow's day cell (visible, enabled, not muted)
            clicked = False
            for sel in [f"td.day:not(.disabled):not(.old):not(.new):text-is('{day}')",
                        f".datepicker-days td:not(.disabled):text-is('{day}')",
                        f"td:not(.disabled):text-is('{day}')"]:
                try:
                    pg.click(sel, timeout=3000)
                    clicked = True
                    print(f"  clicked day via {sel!r}", flush=True)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                print("  could not click a day cell", flush=True)
            pg.wait_for_timeout(1500)

            # click Search
            try:
                pg.click("#UpdateFilerButton", timeout=6000)
            except Exception as e:  # noqa: BLE001
                print(f"  Search click failed: {type(e).__name__}", flush=True)
            # poll for results
            got = 0
            for _ in range(20):
                pg.wait_for_timeout(1000)
                got = pg.evaluate(COUNT_JS)
                if got:
                    break
            print(f"  AFTER search: times={got} url={pg.url[:100]}", flush=True)
            if not got:
                body = pg.evaluate(
                    "() => (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,400)")
                print(f"  body: {body!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {type(e).__name__} {str(e)[:90]}", flush=True)
        finally:
            b.close()


if __name__ == "__main__":
    main()
