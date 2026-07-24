"""Nail mechanics for the 2 buildable niche platforms:
SuperSaaS — navigate to a target date, dump available slots.
Square — select the 'Tee Time' service, capture the availability API + slots."""
from __future__ import annotations
import datetime as dt, json, re

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TMR = dt.date.today() + dt.timedelta(days=1)

def supersaas(pw):
    b = pw.chromium.launch(args=["--no-sandbox"]); 
    try:
        pg = b.new_page(user_agent=UA)
        # SuperSaaS day view via query params
        url = (f"https://www.supersaas.com/schedule/Terry%27s_Golf/SAGC_TEE_TIMES"
               f"?year={TMR.year}&month={TMR.month}&day={TMR.day}&view=day")
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(6000)
        info = pg.evaluate(r"""() => {
          const heading = (document.querySelector("h1,h2,.date,#date")||{}).textContent||"";
          // available slots: SuperSaaS marks free slots as clickable links/cells
          const cells = [...document.querySelectorAll("a,td,div")].filter(e=>{
            const t=(e.textContent||"").trim();
            return /^\d?\d:\d\d\s*(am|pm)/i.test(t) && t.length<40; });
          const rows = cells.slice(0,12).map(e=>({t:(e.textContent||"").replace(/\s+/g," ").trim().slice(0,50),
            cls:(e.className||"").slice(0,40), avail:/avail|free|open/i.test((e.className||"")+(e.title||""))}));
          const full = (document.body.innerText||"").replace(/\s+/g," ");
          return {url: location.href, heading: heading.slice(0,60),
                  dateShown: (full.match(/\w+ \d{1,2} \w+ 2026/)||[])[0],
                  slotCount: cells.length, sample: rows};
        }""")
        print("===== SUPERSAAS =====", flush=True)
        print(json.dumps(info, indent=1)[:1500], flush=True)
    except Exception as e:
        print(f"SUPERSAAS ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally: b.close()

def square(pw):
    b = pw.chromium.launch(args=["--no-sandbox"])
    api = []
    try:
        pg = b.new_page(user_agent=UA)
        def on_resp(resp):
            u=resp.url
            if "square" not in u: return
            if any(x in u for x in ("avail","booking","appointment","search","slot","service")) and "hydrate" not in u:
                ct=(resp.headers.get("content-type") or "").lower()
                if "json" not in ct: return
                try: body=resp.text()
                except Exception: return
                api.append({"u":u[:150],"m":resp.request.method,"status":resp.status,
                            "post":(resp.request.post_data or "")[:300],"body":body[:500]})
        pg.on("response", on_resp)
        pg.goto("https://app.squareup.com/appointments/book/gx20u6dqwo3xjm/LX8E0VBSXSYKF/start",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(5000)
        # click a "Tee Time" service to trigger availability
        clicked=False
        for txt in ["9 Hole Tee Time","Tee Time","Member Tee Time"]:
            try:
                pg.get_by_text(txt, exact=False).first.click(timeout=4000); clicked=True
                print(f"  clicked service: {txt}", flush=True); break
            except Exception: continue
        pg.wait_for_timeout(6000)
        dom = pg.evaluate(r"""() => {
          const t=(document.body.innerText||"").replace(/\s+/g," ");
          return {times:(t.match(/\d?\d:\d\d\s*(?:AM|PM)/g)||[]).length,
                  snippet:t.slice(0,220)};}""")
        print("===== SQUARE =====", flush=True)
        print(f"  clicked={clicked} DOM={json.dumps(dom)}", flush=True)
        for h in api[:10]:
            print(f"  {h['status']} {h['m']} {h['u']}", flush=True)
            if h['post']: print(f"    POST {h['post']}", flush=True)
            print(f"    BODY {h['body']}", flush=True)
    except Exception as e:
        print(f"SQUARE ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally: b.close()

def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        supersaas(pw); square(pw)

if __name__ == "__main__":
    main()
