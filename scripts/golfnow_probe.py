"""Diagnostic: capture GolfNow's real tee-time API call from a browser.

challenge:false in the earlier probe means GolfNow loads normally in a real
Chromium — we just need to learn which request returns the tee-time JSON so we
can replay it in-page (real browser TLS + origin), the same way we do for
cps.golf / ezlinks. This listens to network traffic on the facility search page
and prints any request whose response looks like tee-time data: URL, method,
request body, response status, top-level JSON keys, and one sample slot.

Read the RESULT lines in the log.
"""
from __future__ import annotations

import datetime as dt
import json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# (facility_id, slug) — the URL is /tee-times/facility/<id>-<slug>/search
FACILITIES = [("453", "arrowhead-golf-club"), ("4719", "pelican-lakes-golf-country-club")]

INTEREST = ("tee-time", "teetime", "cds.golfnow", "tee_time")


def _shape(obj, depth=0):
    """Compact structural summary of a JSON object."""
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]"] + ([_shape(obj[0], depth + 1)] if obj and depth < 3 else [])
    return type(obj).__name__


def probe(pw, fid: str, slug: str, date_iso: str) -> None:
    b = pw.chromium.launch(args=["--no-sandbox"])
    hits: list[dict] = []
    reqs: dict[str, dict] = {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_request(req):
            u = req.url.lower()
            if any(k in u for k in INTEREST):
                try:
                    reqs[req.url] = {"method": req.method,
                                     "post": (req.post_data or "")[:600],
                                     "headers": {k: v for k, v in req.headers.items()
                                                 if k.lower() in
                                                 ("content-type", "accept", "x-requested-with",
                                                  "authorization", "referer")}}
                except Exception:
                    pass

        def on_response(resp):
            u = resp.url.lower()
            if not any(k in u for k in INTEREST):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            try:
                data = resp.json()
            except Exception:
                return
            hits.append({"url": resp.url, "status": resp.status, "data": data})

        pg.on("request", on_request)
        pg.on("response", on_response)
        pg.goto(f"https://www.golfnow.com/tee-times/facility/{fid}-{slug}/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(12000)  # let the SPA fire its tee-time API calls

        print(f"RESULT golfnow-probe {fid}-{slug}: {len(hits)} json hits", flush=True)
        for h in hits:
            key = None
            for k in reqs:
                if k == h["url"]:
                    key = reqs[k]
                    break
            times_n = None
            d = h["data"]
            # find a list of slots anywhere near the top
            if isinstance(d, dict):
                for v in d.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        times_n = len(v)
                        break
            print("  URL:", h["url"][:160], flush=True)
            print("  status:", h["status"], "| req:", json.dumps(key) if key else "n/a", flush=True)
            print("  list_len:", times_n, "| shape:", json.dumps(_shape(d))[:900], flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT golfnow-probe {fid}-{slug}: ERROR {type(e).__name__} {str(e)[:120]}", flush=True)
    finally:
        b.close()


def main() -> None:
    from playwright.sync_api import sync_playwright
    date_iso = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    with sync_playwright() as pw:
        for fid, slug in FACILITIES:
            probe(pw, fid, slug, date_iso)


if __name__ == "__main__":
    main()
