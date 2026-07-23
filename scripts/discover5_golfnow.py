"""Probe #5 — why do 4 GolfNow facilities (black bear 2484, desert hawk 5635,
gypsum creek 3534, tamarack 16424) 404 on the replayed search POST while other
facilities work? Load each page, log the FINAL URL (redirect?), the page's own
search POST endpoint + status + response size, and whether tee times render in
the DOM. That tells us whether the facility moved, the page type differs, or
the predicate needs different fields.
"""
from __future__ import annotations

import json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TARGETS = [
    ("2484", "black-bear-golf-club"),
    ("5635", "desert-hawk-at-pueblo-west"),
    ("3534", "gypsum-creek-golf-course"),
    ("16424", "tamarack-golf-course"),
    ("1411", "the-ridge-at-castle-pines-colorado"),   # control (works)
]


def probe(pw, fid, slug):
    b = pw.chromium.launch(args=["--no-sandbox"])
    posts = []
    try:
        pg = b.new_page(user_agent=UA)

        def on_resp(resp):
            req = resp.request
            if req.method == "POST" and "golfnow.com/api" in req.url:
                entry = {"url": req.url[:120], "status": resp.status}
                try:
                    entry["post"] = (req.post_data or "")[:300]
                    body = resp.text()
                    entry["resp_len"] = len(body)
                    entry["resp_head"] = body[:200]
                except Exception:
                    pass
                posts.append(entry)

        pg.on("response", on_resp)
        pg.goto(f"https://www.golfnow.com/tee-times/facility/{fid}-{slug}/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(12000)
        dom = pg.evaluate("""() => {
          const txt = document.body ? document.body.innerText : "";
          return {title: document.title.slice(0,70),
                  times: (txt.match(/\\d?\\d:\\d\\d\\s*[AP]M/gi)||[]).length,
                  notFound: /404|not found|no longer|unavailable/i.test(txt.slice(0,3000))};
        }""")
        print(f"RESULT gn5 {fid}-{slug}:", flush=True)
        print(f"  finalUrl: {pg.url}", flush=True)
        print(f"  dom: {json.dumps(dom)}", flush=True)
        for p in posts[:8]:
            print(f"  POST {p['status']} {p['url']} resp_len={p.get('resp_len')}",
                  flush=True)
            if p.get("post"):
                print(f"    body: {p['post']}", flush=True)
            if p['status'] != 200 and p.get('resp_head'):
                print(f"    resp: {p['resp_head']!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT gn5 {fid}-{slug}: ERROR {type(e).__name__} {str(e)[:90]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for fid, slug in TARGETS:
            probe(pw, fid, slug)


if __name__ == "__main__":
    main()
