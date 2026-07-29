"""What API does a GolfBack tee-sheet page actually call?

WHY THIS EXISTS
---------------
GolfBack is the largest unsupported platform in the registry: 17 Florida
courses, every one of them already carrying its course uuid (the uuid is right
there in the booking URL, `golfback.com/#/course/<uuid>`), and not one of them
scrapeable because no adapter exists. Writing that adapter needs the request
shape, and the request shape cannot be obtained from outside a browser:

  * golfback.com serves the SPA shell for EVERY path — `/api/course/<uuid>`
    comes back as the same "Tee Times - GolfBack" HTML document — so the app is
    a pure client-side router and its HTML says nothing about its API.
  * api.golfback.com IS a live JSON host: `GET /` returns
    {"happy":"Somebody's closer!","version":"Version 1.4.5.119+edc3"}.
    But it exposes no Swagger and it 404s every guessed route. Seven guesses
    against plausible REST shapes (/api/course/<uuid>, /api/courses/<uuid>,
    /api/teetimes?courseId=..., /api, /swagger/index.html) all missed.

Guessing further is the wrong instrument. This loads the real page in Chromium
and records what the app asks for. That is the same technique browser_clubcaddie
uses to recover the TeeTimes POST body it cannot construct from scratch.

WHAT IT RECORDS
---------------
Every XHR/fetch the page issues, with method, full URL, request headers that
look load-bearing (auth, tenant, api-key, content-type), any POST body, and the
response status plus a bounded sample of the body. Then it changes the date in
the UI and records again, because the DIFFERENCE between two dates is what
names the date parameter — the single most important thing an adapter needs and
the thing a single capture cannot reveal.

Two courses on purpose, not one: a value appearing in both captures is part of
the platform's shape, while a value appearing in only one is that course's
identifier. One capture cannot tell those apart, and mistaking a course id for
a constant is how an adapter ends up publishing one club's sheet under every
name.

Report only. No D1 writes, no registry or CSV edits, GET/POST observation of a
public page — nothing is submitted and no booking step is touched.

  python scripts/browser_golfback_probe.py
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Two FL courses from the registry, deliberately different tenants so a
# per-course id can be told apart from a platform constant. Blackwater is a
# single municipal course; Mission Resort is a 36-hole resort whose two courses
# are separate uuids, which also shows whether the API is course- or
# facility-scoped.
TARGETS = [
    ("blackwater-golf-club", "1c05615c-f127-4d81-8aec-fe339abd31f8"),
    ("mission-resort-club-el-campeon-course",
     "5379e760-2a4b-4d0a-8b35-c619b8f3f32c"),
]

OUT = "probe-results/golfback-api.json"

# Headers worth reporting. A blanket dump would bury the signal in Accept-*
# and sec-* noise, and would also risk echoing a cookie into a committed file.
HEADER_KEYS = re.compile(
    r"^(authorization|x-api-key|apikey|x-tenant|tenant|x-course|x-client|"
    r"x-requested-with|content-type|origin|referer|accept)$", re.I)

# Anything that looks like a bearer token or session cookie is redacted rather
# than committed. The probe needs to know a header EXISTS and what shape it has,
# never its secret value.
def _safe(name: str, value: str) -> str:
    if name.lower() in ("authorization", "x-api-key", "apikey", "cookie"):
        head = value.split(" ")[0] if " " in value else ""
        return f"<redacted len={len(value)}{' scheme=' + head if head else ''}>"
    return value[:200]


def _interesting(url: str) -> bool:
    """XHR/fetch traffic to a data host, not assets or analytics."""
    if re.search(r"\.(js|css|png|jpe?g|svg|woff2?|ico|gif|map)(\?|$)", url, re.I):
        return False
    return not re.search(
        r"googletagmanager|google-analytics|doubleclick|facebook|hotjar|"
        r"sentry|clarity\.ms|gstatic|fonts\.google", url, re.I)


def capture(pw, slug: str, uuid: str) -> dict:
    rec: dict = {"slug": slug, "uuid": uuid, "phases": {}}
    browser = pw.chromium.launch(args=["--no-sandbox"])
    try:
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        calls: list[dict] = []

        def on_response(resp):
            rq = resp.request
            if rq.resource_type not in ("xhr", "fetch"):
                return
            if not _interesting(rq.url):
                return
            entry = {
                "method": rq.method,
                "url": rq.url,
                "status": resp.status,
                "headers": {k: _safe(k, v) for k, v in rq.headers.items()
                            if HEADER_KEYS.match(k)},
            }
            try:
                if rq.post_data:
                    entry["post_data"] = rq.post_data[:1500]
            except Exception:
                pass
            try:
                body = resp.text()
                entry["body_bytes"] = len(body)
                entry["body_sample"] = body[:1800]
            except Exception as e:                       # noqa: BLE001
                # A body that cannot be read is recorded as such. Recording it
                # as empty would be a different and false claim.
                entry["body_error"] = f"{type(e).__name__}: {e}"[:160]
            calls.append(entry)

        page.on("response", on_response)

        page.goto(f"https://golfback.com/#/course/{uuid}",
                  wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)
        rec["phases"]["load"] = list(calls)
        rec["title"] = page.title()

        # Phase two: move the date. Whatever parameter changes between the two
        # phases is the date parameter — the thing a single capture cannot name.
        calls.clear()
        moved = None
        for sel in ('button[aria-label*="next" i]', 'button:has-text("Next")',
                    '[class*="next"]', 'input[type="date"]'):
            try:
                el = page.query_selector(sel)
                if el:
                    if sel == 'input[type="date"]':
                        el.fill("2026-08-15")
                    else:
                        el.click()
                    moved = sel
                    break
            except Exception:
                continue
        page.wait_for_timeout(6000)
        rec["date_control"] = moved or "NOT FOUND — no date control matched"
        rec["phases"]["after_date_change"] = list(calls)

        hosts = sorted({re.sub(r"^https?://([^/]+).*", r"\1", c["url"])
                        for p in rec["phases"].values() for c in p})
        rec["hosts_seen"] = hosts
        return rec
    finally:
        browser.close()


def main() -> int:
    out: dict = {"targets": []}
    with sync_playwright() as pw:
        for slug, uuid in TARGETS:
            print("=" * 72)
            print(f"{slug}  uuid={uuid}")
            print("=" * 72, flush=True)
            try:
                rec = capture(pw, slug, uuid)
            except Exception as e:                       # noqa: BLE001
                rec = {"slug": slug, "uuid": uuid,
                       "error": f"{type(e).__name__}: {e}"[:400]}
                print(f"  FAILED {rec['error']}", flush=True)
            out["targets"].append(rec)
            if "error" in rec:
                continue
            print(f"  title: {rec.get('title')}")
            print(f"  date control: {rec.get('date_control')}")
            print(f"  hosts: {rec.get('hosts_seen')}")
            for phase, calls in rec["phases"].items():
                print(f"  -- {phase}: {len(calls)} xhr/fetch calls")
                for c in calls:
                    print(f"     {c['method']:5s} {c['status']} {c['url'][:150]}")
                    if c.get("post_data"):
                        print(f"           body: {c['post_data'][:200]}")
                    if c.get("body_sample"):
                        print(f"           resp: {c['body_sample'][:240]}")
            print(flush=True)

    # Cross-course comparison: a URL path shape both courses call is platform
    # shape; one only a single course calls carries that course's identity.
    def paths(rec):
        return {re.sub(r"\?.*$", "", c["url"])
                for p in rec.get("phases", {}).values() for c in p}
    recs = [r for r in out["targets"] if "phases" in r]
    if len(recs) == 2:
        a, b = paths(recs[0]), paths(recs[1])
        out["shared_paths"] = sorted(a & b)
        out["per_course_paths"] = {recs[0]["slug"]: sorted(a - b),
                                   recs[1]["slug"]: sorted(b - a)}
        print("SHARED paths (platform shape):")
        for p in out["shared_paths"]:
            print("   ", p)
        print("PER-COURSE paths (carry course identity):")
        for slug, ps in out["per_course_paths"].items():
            for p in ps:
                print(f"    {slug}: {p}")

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
