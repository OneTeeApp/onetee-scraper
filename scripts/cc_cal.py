"""Probe v2: drive the DTP material datepicker properly.
Day cells are span.dtp-select-day. Open picker, click the target day, confirm,
Search, and capture the network response that carries the tee times.
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
  return all.filter(e=>e.children.length===0 && /^\s*\d?\d:\d\d\s*[AP]M\s*$/i.test(e.textContent||"")).length;
}"""

DTP_JS = r"""() => {
  const dtp=[...document.querySelectorAll(".dtp,[class*=dtp]")].filter(e=>e.offsetParent!==null);
  return dtp.slice(0,1).map(e=>e.outerHTML.replace(/\s+/g," ").slice(0,1200));
}"""


def run(pw, c, day_num):
    base = f"https://apimanager-{c['ids']['shard']}.clubcaddie.com"
    token = c["ids"]["view_token"]
    b = pw.chromium.launch(args=["--no-sandbox"])
    data_resps = []
    try:
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()

        def on_resp(resp):
            u = resp.url
            if "clubcaddie" in u and resp.request.method in ("GET", "POST") \
                    and not u.endswith((".js", ".css", ".png", ".woff2", ".jpg", ".svg")):
                try:
                    t = resp.text()
                except Exception:
                    return
                # does this response carry tee-time strings?
                import re
                n = len(re.findall(r"\d?\d:\d\d\s*[AP]M", t))
                if n >= 3:
                    data_resps.append({"url": u.split(".com", 1)[-1][:110],
                                       "status": resp.status, "times": n,
                                       "ct": (resp.headers.get("content-type") or "")[:20]})

        pg.on("response", on_resp)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(7000)
        print(f"RESULT {c['slug']} (day {day_num}): baseline={pg.evaluate(COUNT_JS)}", flush=True)

        # open picker
        for opener in ("#datechange", "#dateinput"):
            try:
                pg.click(opener, timeout=4000)
                pg.wait_for_timeout(1200)
                break
            except Exception:  # noqa: BLE001
                continue
        print(f"  DTP overlay: {json.dumps(pg.evaluate(DTP_JS))[:600]}", flush=True)

        # click the day span (try unpadded + padded)
        clicked = False
        for txt in (str(day_num), f"{day_num:02d}"):
            for sel in (f"span.dtp-select-day:text-is('{txt}')",
                        f".dtp-picker-days span:text-is('{txt}')",
                        f"a.dtp-select-day:text-is('{txt}')"):
                try:
                    pg.click(sel, timeout=2500)
                    clicked = True
                    print(f"  clicked day via {sel!r}", flush=True)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if clicked:
                break
        pg.wait_for_timeout(1000)
        # confirm/OK if present
        for sel in ("a.dtp-btn-ok", ".dtp-btn-ok", "button:has-text('OK')",
                    ".dtp-buttons a:has-text('OK')"):
            try:
                pg.click(sel, timeout=1500)
                print(f"  clicked OK via {sel!r}", flush=True)
                break
            except Exception:  # noqa: BLE001
                continue
        pg.wait_for_timeout(800)
        # value now?
        val = pg.eval_on_selector("#dateinput", "e=>e.value") if clicked else "?"
        print(f"  #dateinput value after pick: {val!r}", flush=True)

        # Search
        try:
            pg.click("#UpdateFilerButton", timeout=6000)
        except Exception as e:  # noqa: BLE001
            print(f"  Search failed: {type(e).__name__}", flush=True)
        got = 0
        for _ in range(20):
            pg.wait_for_timeout(1000)
            got = pg.evaluate(COUNT_JS)
            if got:
                break
        print(f"  AFTER search: times={got} url={pg.url[:95]}", flush=True)
        print(f"  data responses w/ times: {json.dumps(data_resps[:6])}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT {c['slug']}: ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    courses = [x for x in reg if x["slug"] in ("salida-golf-club", "applewood-golf-course")]
    with sync_playwright() as pw:
        for c in courses:
            run(pw, c, TOMORROW.day)


if __name__ == "__main__":
    main()
