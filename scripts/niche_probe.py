"""Final mechanics: SuperSaaS available-vs-booked markup (date ~10d out for more
availability); Square full flow to reach times + capture the availability API."""
from __future__ import annotations
import datetime as dt, json
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
D = dt.date.today() + dt.timedelta(days=9)

def supersaas(pw):
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        pg = b.new_page(user_agent=UA)
        pg.goto(f"https://www.supersaas.com/schedule/Terry%27s_Golf/SAGC_TEE_TIMES"
                f"?year={D.year}&month={D.month}&day={D.day}&view=day",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(6000)
        # dump the schedule area's HTML + classify cells
        info = pg.evaluate(r"""() => {
          // find elements whose text is a time-range and report tag/class/title/onclick
          const all=[...document.querySelectorAll("*")];
          const slots=[];
          for (const e of all){
            const t=(e.textContent||"").trim();
            if(/^\d?\d:\d\d\s*(am|pm)\s*[−-]\s*\d?\d:\d\d/i.test(t) && e.children.length<=2){
              slots.push({tag:e.tagName, cls:e.className, title:e.getAttribute("title")||"",
                          href:e.getAttribute("href")||"", clickable:!!e.onclick||e.tagName==="A",
                          txt:t.replace(/\s+/g," ").slice(0,40)});
            }
          }
          // also count "free"/bookable markers
          const free=document.querySelectorAll(".free,.available,.bookable,td.f0,.slot.free").length;
          return {dateShown:(document.body.innerText.match(/\w+day \d{1,2} \w+ 2026/)||[])[0],
                  totalSlots:slots.length, free, sample:slots.slice(0,10)};
        }""")
        print("===== SUPERSAAS (date "+D.isoformat()+") =====", flush=True)
        print(json.dumps(info, indent=1)[:1800], flush=True)
    except Exception as e:
        print(f"SUPERSAAS ERR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally: b.close()

def square(pw):
    b = pw.chromium.launch(args=["--no-sandbox"]); api=[]
    try:
        pg = b.new_page(user_agent=UA)
        def on_resp(resp):
            u=resp.url.lower()
            if "square" not in u or "hydrate" in u: return
            ct=(resp.headers.get("content-type") or "").lower()
            if "json" not in ct: return
            try: body=resp.text()
            except Exception: return
            if any(k in u for k in ("avail","slot","time","booking","search")) or "start_at" in body or "startAt" in body:
                api.append({"u":resp.url[:150],"m":resp.request.method,"status":resp.status,
                            "post":(resp.request.post_data or "")[:250],"body":body[:600]})
        pg.on("response", on_resp)
        pg.goto("https://app.squareup.com/appointments/book/gx20u6dqwo3xjm/LX8E0VBSXSYKF/start",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(4000)
        steps=[]
        for label in ["9 Hole Tee Time","Individual","Continue","Next"]:
            try:
                pg.get_by_text(label, exact=False).first.click(timeout=3500)
                steps.append(label); pg.wait_for_timeout(3500)
            except Exception: pass
        pg.wait_for_timeout(4000)
        dom = pg.evaluate(r"""()=>{const t=(document.body.innerText||"").replace(/\s+/g," ");
          return {times:(t.match(/\d?\d:\d\d\s*(?:AM|PM)/g)||[]).length, snippet:t.slice(0,200)};}""")
        print("===== SQUARE =====", flush=True)
        print(f"  steps={steps} DOM={json.dumps(dom)}", flush=True)
        for h in api[:8]:
            print(f"  {h['status']} {h['m']} {h['u']}", flush=True)
            if h['post']: print(f"    POST {h['post']}", flush=True)
            print(f"    BODY {h['body']}", flush=True)
    except Exception as e:
        print(f"SQUARE ERR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally: b.close()

def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        supersaas(pw); square(pw)
if __name__=="__main__": main()
