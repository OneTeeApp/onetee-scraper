"""Discover the data endpoint for the 3 niche platforms so we can build
adapters: SuperSaaS (Spreading Antlers), Square Appointments (Cottonwood Links),
goibsvision WebRes (Copper Creek / Telluride). Browser capture of network JSON
+ DOM, plus plain-HTTP probes of likely feeds."""
from __future__ import annotations
import datetime as dt, json, re, sys
import requests
sys.path.insert(0, ".")
from scraper.adapters.base import USER_AGENT

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TMR = (dt.date.today() + dt.timedelta(days=1))

def capture(pw, name, url, wait=12000):
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits = []
    try:
        pg = b.new_page(user_agent=UA)
        def on_resp(resp):
            u = resp.url
            if any(x in u for x in (".js",".css",".png",".svg",".woff",".jpg",".gif","gtm","analytics","hotjar","sentry","fonts")): return
            ct = (resp.headers.get("content-type") or "").lower()
            if not any(t in ct for t in ("json","xml","javascript")): return
            try: body = resp.text()
            except Exception: return
            has_time = len(re.findall(r"\d?\d:\d\d", body))
            if has_time >= 2 or "avail" in u.lower() or "book" in u.lower() or "teetime" in u.lower() or "slot" in u.lower():
                hits.append({"m": resp.request.method, "u": u[:180], "ct": ct[:20],
                             "status": resp.status, "len": len(body), "times": has_time,
                             "post": (resp.request.post_data or "")[:200], "body": body[:600]})
        pg.on("response", on_resp)
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(wait)
        dom = pg.evaluate("""() => {
          const t=(document.body&&document.body.innerText||"");
          return {title: document.title.slice(0,50),
                  times:(t.match(/\\d?\\d:\\d\\d\\s*(?:AM|PM|am|pm)/g)||[]).length,
                  needsLogin: /log ?in|sign ?in|must login/i.test(t.slice(0,600)),
                  snippet: t.replace(/\\s+/g," ").slice(0,200)};}""")
        print(f"\n===== {name} =====\n  {url}", flush=True)
        print(f"  DOM: {json.dumps(dom)}", flush=True)
        for h in hits[:10]:
            print(f"  {h['status']} {h['m']} times={h['times']} {h['u']}", flush=True)
            if h["post"]: print(f"    POST: {h['post']}", flush=True)
            print(f"    BODY: {h['body']}", flush=True)
    except Exception as e:
        print(f"===== {name}: ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally:
        b.close()

def plain():
    s = requests.Session(); s.headers["User-Agent"] = USER_AGENT
    print("\n===== SuperSaaS plain feeds =====", flush=True)
    base = "https://www.supersaas.com/schedule/Terry's_Golf/SAGC_TEE_TIMES"
    for suffix, note in [(".json","json"), (f"/{TMR.isoformat()}","day"),
                         ("/free.json","free"), (".ics","ics")]:
        try:
            r = s.get(base+suffix, timeout=20)
            print(f"  {note:<6} {r.status_code} len={len(r.text)} head={r.text[:120]!r}", flush=True)
        except Exception as e:
            print(f"  {note}: FAIL {type(e).__name__}", flush=True)

def main():
    from playwright.sync_api import sync_playwright
    plain()
    d = TMR.isoformat()
    with sync_playwright() as pw:
        capture(pw, "supersaas spreading-antlers",
                "https://www.supersaas.com/schedule/Terry%27s_Golf/SAGC_TEE_TIMES")
        capture(pw, "square cottonwood-links",
                "https://app.squareup.com/appointments/book/gx20u6dqwo3xjm/LX8E0VBSXSYKF/start")
        capture(pw, "goibs copper-creek",
                "https://www.goibsvision.com/WebRes/Club/ccgc/Browse")
        capture(pw, "goibs telluride",
                "https://www.goibsvision.com/WebRes/Club/Telluride/Browse")

if __name__ == "__main__":
    main()
