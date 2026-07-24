"""Probe v3: capture the /webapi/TeeTimes request (full URL, method, params,
headers) and its HTML response, then replay it in-page for tomorrow. This is
the endpoint the widget fetches on load — the real data source.
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


def run(pw, c):
    base = f"https://apimanager-{c['ids']['shard']}.clubcaddie.com"
    token = c["ids"]["view_token"]
    b = pw.chromium.launch(args=["--no-sandbox"])
    seen = {}
    try:
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()

        def on_resp(resp):
            if "/webapi/TeeTimes" in resp.url:
                req = resp.request
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                seen.update({
                    "url": resp.url,
                    "method": req.method,
                    "post": (req.post_data or "")[:400],
                    "req_headers": {k: v for k, v in req.headers.items()
                                    if k.lower() in ("x-requested-with", "accept",
                                    "referer", "content-type")},
                    "status": resp.status,
                    "body": body,
                })

        pg.on("response", on_resp)
        pg.goto(f"{base}/webapi/view/{token}", wait_until="networkidle", timeout=40000)
        pg.wait_for_timeout(6000)

        print(f"RESULT {c['slug']}:", flush=True)
        if not seen:
            print("  /webapi/TeeTimes NOT observed", flush=True)
            return
        print(f"  URL: {seen['url'][:200]}", flush=True)
        print(f"  method={seen['method']} post={seen['post']!r}", flush=True)
        print(f"  req_headers={json.dumps(seen['req_headers'])}", flush=True)
        body = seen["body"]
        import re
        times = re.findall(r"\d?\d:\d\d\s*[AP]M", body)
        print(f"  resp: status={seen['status']} len={len(body)} times={len(times)}",
              flush=True)
        # show a chunk of HTML around the first time to design the parser
        m = re.search(r"\d?\d:\d\d\s*[AP]M", body)
        if m:
            start = max(0, m.start() - 400)
            print(f"  HTML around first slot: {body[start:m.start()+500]}", flush=True)

        # replay for tomorrow: swap the date param in the captured URL
        url = seen["url"]
        import urllib.parse as up
        parsed = up.urlparse(url)
        q = dict(up.parse_qsl(parsed.query))
        # find the date-ish param
        datekeys = [k for k in q if "date" in k.lower()]
        print(f"  date params: {[(k, q[k]) for k in datekeys]}", flush=True)
        for k in datekeys:
            q[k] = TOMORROW.strftime("%m/%d/%Y")
        newq = up.urlencode(q)
        replay_url = up.urlunparse(parsed._replace(query=newq))
        r = pg.evaluate(
            """async ([u]) => {
              const r = await fetch(u, {headers:{"X-Requested-With":"XMLHttpRequest"}});
              const t = await r.text();
              const n = (t.match(/\\d?\\d:\\d\\d\\s*[AP]M/g)||[]).length;
              return {status:r.status, len:t.length, times:n, isHtml:t.trim()[0]==="<"};
            }""", [replay_url])
        print(f"  REPLAY tomorrow ({TOMORROW.isoformat()}): {json.dumps(r)}", flush=True)
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
            run(pw, c)


if __name__ == "__main__":
    main()
