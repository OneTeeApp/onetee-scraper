"""Probe #7 — last two candidates.

1. SuperSaaS (Spreading Antlers): fetch the schedule page PLAIN and find where
   slot data lives (inline JSON? ajax path?). The browser rendered 30 times, so
   the data is served to anonymous clients — question is just the format.
   Also try the known ajax endpoints for free schedules.
2. Eagle Trace GolfNow resale (2610): does the facility page have a tee sheet?
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys

import requests

sys.path.insert(0, ".")
from scraper.adapters.base import USER_AGENT  # noqa: E402

SCHED = "https://www.supersaas.com/schedule/Terry%27s_Golf/SAGC_TEE_TIMES"

UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def supersaas_plain():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    r = s.get(SCHED, timeout=25)
    html = r.text
    print(f"RESULT ss page: {r.status_code} len={len(html)}", flush=True)
    # schedule id + config hints
    for pat in (r"schedule_id\D{0,5}(\d+)", r'"schedule"\s*:\s*(\d+)',
                r"new_appointment[^\"']{0,60}", r"/ajax/\w+", r"api_key\W{0,3}\w+",
                r'data-\w+="[^"]{0,60}"'):
        m = re.findall(pat, html)
        if m:
            print(f"  pat {pat!r}: {m[:6]}", flush=True)
    # any inline JSON blobs with times?
    times_inline = re.findall(r"\d{1,2}:\d{2}\s*[ap]m", html, re.I)
    print(f"  inline time strings: {len(times_inline)}", flush=True)
    scripts = re.findall(r"<script[^>]*src=\"([^\"]+)\"", html)
    print(f"  scripts: {scripts[:6]}", flush=True)

    # known free-schedule data endpoints
    for url, note in [
        ("https://www.supersaas.com/api/bookings.json?schedule=SAGC_TEE_TIMES&account=Terry%27s_Golf", "public api"),
        (SCHED + ".ics", "ics feed"),
        (SCHED + "/month", "month view"),
    ]:
        try:
            rr = s.get(url, timeout=20)
            print(f"RESULT ss {note}: {rr.status_code} len={len(rr.text)} "
                  f"head={rr.text[:150]!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT ss {note}: FAIL {type(e).__name__}", flush=True)


def supersaas_browser_xhr():
    """Watch ALL requests (any host) while the schedule renders, to catch the
    data call we missed with a narrow filter."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA_BROWSER)
            urls = []
            pg.on("response", lambda r: urls.append(
                (r.status, r.request.method, r.url[:130],
                 (r.headers.get("content-type") or "")[:30])))
            pg.goto(SCHED, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(9000)
            print("RESULT ss xhr trail:", flush=True)
            for st, m, u, ct in urls:
                if any(x in ct for x in ("json", "javascript", "xml", "html")) \
                        and "assets" not in u and "cdn" not in u:
                    print(f"  {st} {m} {u} [{ct}]", flush=True)
            # dump a slot's DOM text for parser design
            slot_txt = pg.evaluate("""() => {
              const t = (document.body.innerText.match(/\\d{1,2}:\\d{2}[ap]m[^\\n]{0,80}/gi)||[]);
              return t.slice(0, 8);
            }""")
            print(f"  slots text: {json.dumps(slot_txt)}", flush=True)
        finally:
            b.close()


def golfnow_2610():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA_BROWSER)
            pg.goto("https://www.golfnow.com/tee-times/facility/2610-eagle-trace-golf-club/search",
                    wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(9000)
            r = pg.evaluate("""() => {
              const txt = (document.body && document.body.innerText || "");
              return {title: document.title.slice(0,60),
                      times: (txt.match(/\\d?\\d:\\d\\d\\s*[AP]M/gi)||[]).length};
            }""")
            print(f"RESULT golfnow 2610-eagle-trace: {json.dumps(r)}", flush=True)
        finally:
            b.close()


if __name__ == "__main__":
    supersaas_plain()
    supersaas_browser_xhr()
    golfnow_2610()
