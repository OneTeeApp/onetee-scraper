"""Probe #4 — final verification of the booking-system remap.

1. Meeker: load the ForeUp booking page in a browser and capture the SPA's own
   API calls (the static page is a 1KB stub; the config must load via XHR).
2. Adapter spot-checks (plain HTTP): granby teeitup (with /alias fallback),
   patty-jewett + valley-hi foreup (pinned booking_class), dalton foretees.
3. Full browser fetchers for tomorrow: cps (now incl. westminster x2 +
   marianabutte), ezlinks (now incl. heritageeaglebendnrpp), golfnow (now
   incl. 4 new facilities + black bear slug fix). Per-course summaries.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import sys

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
TOMORROW = dt.date.today() + dt.timedelta(days=1)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def meeker_capture(pw):
    b = pw.chromium.launch(args=["--no-sandbox"])
    calls = []
    try:
        pg = b.new_page(user_agent=UA)

        def on_resp(resp):
            u = resp.url
            if "foreupsoftware" in u and ("api" in u or "booking" in u) \
                    and not u.endswith((".js", ".css", ".png", ".woff2")):
                entry = {"url": u[:170], "status": resp.status}
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" in ct:
                    try:
                        entry["body"] = json.dumps(resp.json())[:700]
                    except Exception:
                        pass
                calls.append(entry)

        pg.on("response", on_resp)
        pg.goto("https://foreupsoftware.com/index.php/booking/22597#/teetimes",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(10000)
        txt = pg.evaluate("() => (document.body ? document.body.innerText : '')"
                          ".replace(/\\s+/g,' ').slice(0,250)")
        print(f"RESULT meeker-browser: {len(calls)} api calls, body={txt!r}",
              flush=True)
        for c in calls[:12]:
            print(f"  {c['status']} {c['url']}", flush=True)
            if c.get("body"):
                print(f"    {c['body']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT meeker-browser: ERROR {type(e).__name__} {str(e)[:80]}",
              flush=True)
    finally:
        b.close()


def adapter_checks():
    from scraper.aggregate import load_registry
    from scraper.adapters.teeitup import TeeItUpAdapter
    from scraper.adapters.foreup import ForeUpAdapter
    from scraper.adapters.foretees import ForeTeesAdapter

    reg = load_registry("registry.json")
    by_slug = {c["slug"]: c for c in reg}
    checks = [
        (TeeItUpAdapter(), "golf-granby-ranch"),
        (ForeUpAdapter(), "patty-jewett-golf-course"),
        (ForeUpAdapter(), "valley-hi-golf-course"),
        (ForeTeesAdapter(), "dalton-ranch-golf-club"),
    ]
    for ad, slug in checks:
        try:
            tts = ad.fetch(by_slug[slug], TOMORROW)
            eg = tts[0].to_dict() if tts else None
            print(f"RESULT check {slug}: OK {len(tts)} times "
                  f"e.g. {eg and (eg['teetime'], eg['price_min'])}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT check {slug}: FAIL {type(e).__name__} {str(e)[:120]}",
                  flush=True)


def summarize(name, doc):
    times = doc.get("tee_times") or []
    counts = collections.Counter(t["course_slug"] for t in times)
    print(f"== {name}: ok {doc.get('courses_ok')}/{doc.get('courses_queried')} "
          f"times {len(times)} ==", flush=True)
    for slug, n in sorted(counts.items()):
        print(f"  {slug:<40} {n}", flush=True)
    for e in (doc.get("errors") or []):
        print(f"  ERR {e['course']:<36} {e['error']}", flush=True)


def main():
    from playwright.sync_api import sync_playwright
    from scraper import browser_cps, browser_ezlinks, browser_golfnow

    adapter_checks()
    with sync_playwright() as pw:
        meeker_capture(pw)
    d = TOMORROW
    summarize("CPS", browser_cps.run(d, "registry.json", "output/v_cps.json"))
    summarize("EZLINKS", browser_ezlinks.run(d, "registry.json", "output/v_ez.json"))
    summarize("GOLFNOW", browser_golfnow.run(d, "registry.json", "output/v_gn.json"))


if __name__ == "__main__":
    main()
