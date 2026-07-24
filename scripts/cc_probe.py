"""Probe v5: intercept Club Caddie's own /slots call via route.fetch so we read
the exact request headers AND the response body the page itself receives — the
definitive test of whether the widget gets JSON, and what to replicate.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

sys.path.insert(0, ".")
from scraper.aggregate import load_registry  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def probe(pw, course):
    ids = course["ids"]
    shard, token = ids.get("shard"), ids.get("view_token")
    base = f"https://apimanager-{shard}.clubcaddie.com"
    b = pw.chromium.launch(args=["--no-sandbox"])
    captured = []
    try:
        ctx = b.new_context(user_agent=UA)

        def handle(route):
            req = route.request
            if "/slots" in req.url:
                try:
                    resp = route.fetch()
                    body = resp.text()
                    captured.append({
                        "url": req.url.split(".com", 1)[-1][:130],
                        "req_headers": {k: v for k, v in req.headers.items()
                                        if k.lower() in ("x-requested-with", "accept",
                                        "referer", "cookie", "x-interaction",
                                        "authorization")},
                        "status": resp.status,
                        "ct": (resp.headers.get("content-type") or "")[:30],
                        "is_json": body.strip()[:1] in "{[",
                        "body_head": body[:600],
                    })
                    route.fulfill(response=resp, body=body)
                    return
                except Exception as e:  # noqa: BLE001
                    captured.append({"url": req.url[:80], "err": str(e)[:60]})
            route.continue_()

        ctx.route("**/*", handle)
        pg = ctx.new_page()
        pg.goto(f"{base}/webapi/view/{token}", wait_until="domcontentloaded",
                timeout=40000)
        pg.wait_for_timeout(9000)
        # also read the rendered DOM for tee-time text (fallback plan)
        dom = pg.evaluate(r"""() => {
          const txt = document.body ? document.body.innerText : "";
          const lines = txt.split("\n").map(s=>s.trim()).filter(Boolean);
          const times = lines.filter(l => /\d?\d:\d\d\s*[AP]M/i.test(l));
          return {timeCount: times.length, samples: times.slice(0,8)};
        }""")
        print(f"RESULT cc {course['slug']}:", flush=True)
        for c in captured:
            print(f"  {json.dumps(c)[:900]}", flush=True)
        print(f"  DOM times: {json.dumps(dom)[:500]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT cc {course['slug']}: ERROR {type(e).__name__} {str(e)[:90]}",
              flush=True)
    finally:
        b.close()


def main():
    from playwright.sync_api import sync_playwright
    reg = load_registry("registry.json")
    ccs = [c for c in reg if c["platform"] == "clubcaddie" and c["ids"].get("shard")]
    want = [c for c in ccs if c["slug"] in ("applewood-golf-course", "salida-golf-club")]
    with sync_playwright() as pw:
        for c in want:
            probe(pw, c)


if __name__ == "__main__":
    main()
