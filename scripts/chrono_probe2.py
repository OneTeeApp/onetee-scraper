"""Capture the network calls chronogolf.com's club page makes for an EMPTY
club (troon-north) vs a WORKING one (laughlin-ranch). The v1 marketplace API
returns 0 slots for the empty clubs, so their availability must come from a
different (v2?) endpoint — find it."""
from __future__ import annotations
import datetime as dt, json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DATE = (dt.date.today() + dt.timedelta(days=1)).isoformat()

def probe(pw, slug):
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits = []
    try:
        pg = b.new_page(user_agent=UA)
        def on_resp(resp):
            u = resp.url
            if "chronogolf" not in u and "lightspeed" not in u: return
            if any(x in u for x in (".js", ".css", ".png", ".svg", ".woff", ".jpg", "hotjar", "analytics", "gtm")): return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct: return
            try: body = resp.text()
            except Exception: return
            hits.append({"m": resp.request.method, "u": u[:170],
                         "status": resp.status, "len": len(body),
                         "body": body[:400]})
        pg.on("response", on_resp)
        pg.goto(f"https://www.chronogolf.com/club/{slug}?date={DATE}",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(12000)
        dom = pg.evaluate("""() => {
          const t=(document.body&&document.body.innerText||"");
          return {title: document.title.slice(0,60),
                  times: (t.match(/\\d?\\d:\\d\\d\\s*(?:AM|PM|am|pm)/g)||[]).length,
                  snippet: t.replace(/\\s+/g," ").slice(0,220)};
        }""")
        print(f"\n===== {slug} =====", flush=True)
        print(f"  DOM: {json.dumps(dom)}", flush=True)
        for h in hits[:18]:
            print(f"  {h['status']} {h['m']} {h['u']} len={h['len']}", flush=True)
            if "teetime" in h["u"].lower() or "tee_time" in h["u"].lower() or h["len"] > 3000:
                print(f"    BODY: {h['body']}", flush=True)
    except Exception as e:
        print(f"===== {slug}: ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally:
        b.close()

def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        probe(pw, "laughlin-ranch-golf-course")   # working control
        probe(pw, "troon-north-golf-club")        # empty
        probe(pw, "the-foothills-golf-club")      # empty
if __name__ == "__main__":
    main()
