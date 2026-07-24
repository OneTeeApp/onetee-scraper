"""Load the Chronogolf WIDGET for an empty club and a working control, capture
the teetimes API call the widget fires (URL + all params) + its response, so we
learn the exact endpoint / course id / affiliation that returns availability."""
from __future__ import annotations
import datetime as dt, json
from urllib.parse import urlparse, parse_qs

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# (slug, club_id)
CLUBS = [("laughlin-ranch", 2277), ("troon-north", 2430),
         ("the-foothills", 2403), ("westin-kierland", 2449)]

def probe(pw, name, club_id):
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits = []
    try:
        pg = b.new_page(user_agent=UA)
        def on_resp(resp):
            u = resp.url
            if "teetime" not in u.lower() and "tee_time" not in u.lower(): return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct: return
            try: body = resp.text()
            except Exception: return
            hits.append({"u": u[:220], "q": parse_qs(urlparse(u).query),
                         "status": resp.status, "len": len(body), "body": body[:500]})
        pg.on("response", on_resp)
        pg.goto(f"https://www.chronogolf.com/club/{club_id}/widget?medium=widget&source=club",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(13000)
        dom = pg.evaluate("""() => {
          const t=(document.body&&document.body.innerText||"");
          return {times:(t.match(/\\d?\\d:\\d\\d\\s*(?:AM|PM|am|pm)/g)||[]).length,
                  snippet:t.replace(/\\s+/g," ").slice(0,160)};}""")
        print(f"\n===== {name} (club {club_id}) =====", flush=True)
        print(f"  DOM: {json.dumps(dom)}", flush=True)
        for h in hits[:10]:
            print(f"  {h['status']} len={h['len']} {h['u']}", flush=True)
            print(f"    params: {json.dumps(h['q'])[:300]}", flush=True)
            print(f"    body: {h['body']}", flush=True)
    except Exception as e:
        print(f"===== {name}: ERROR {type(e).__name__} {str(e)[:80]}", flush=True)
    finally:
        b.close()

def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for name, cid in CLUBS:
            probe(pw, name, cid)

if __name__ == "__main__":
    main()
