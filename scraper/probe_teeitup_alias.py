"""Discover the REAL kenna alias + facility_id for TeeItUp courses whose registry
alias (derived from the vanity booking host) 404s at kenna.

WHY. A batch of FL TeeItUp rows sit at needs_ids because their vanity booking
host (e.g. capri-isles-golf-club.book.teeitup.com) is NOT the kenna x-be-alias —
`/alias/<vanity>/facilities` 404s, so fetch() can never address the sheet
(build_registry.py PROBED_HOLDS documents each). But the booking PAGE works: it
loads a real tenant and fires `/v2/tee-times?facilityIds=<n>` to
phx-api-be-east-1b.kenna.io with the REAL alias in the `x-be-alias` request
HEADER. Read that header off the page and we have the alias.

HOW. Drive the booking page in Chromium and listen on `page.on("request")`.
Headers are captured at request-SEND, so this works even from a datacenter runner
where kenna would block the response — we only need the outgoing header + the
facilityIds in the request URL. Output: slug -> {vanity, alias, facility_id}, to
paste into build_registry.py EXTRA_IDS as {"alias": ..., "facility_id": ...}.

Usage:
    python -m scraper.probe_teeitup_alias                 # all teeitup needs_ids
    python -m scraper.probe_teeitup_alias --slugs a,b,c   # a subset
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from urllib.parse import urlparse

from .aggregate import load_registry


def _vanity(booking_url: str) -> str | None:
    """The booking host's first label = the vanity alias in the URL."""
    try:
        host = urlparse(booking_url).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    # <vanity>.book.teeitup.com / .book.teeitup.golf / .play.teeitup.*
    m = re.match(r"([a-z0-9-]+)\.(?:book|play)\.teeitup\.(?:com|golf)", host)
    return m.group(1) if m else None


def probe_one(pw, vanity: str) -> dict:
    """Load one vanity booking page; capture x-be-alias + facilityIds."""
    got: dict = {"vanity": vanity}
    browser = pw.chromium.launch(args=["--no-sandbox"])
    try:
        page = browser.new_context().new_page()

        def on_request(req):
            if "kenna.io" not in req.url:
                return
            try:
                alias = (req.headers or {}).get("x-be-alias")
            except Exception:  # noqa: BLE001
                alias = None
            if alias:
                got["alias"] = alias
            m = re.search(r"facilityIds=(\d+)", req.url)
            if m:
                got.setdefault("facility_id", m.group(1))

        page.on("request", on_request)
        for host in (f"https://{vanity}.book.teeitup.com/",
                     f"https://{vanity}.book.teeitup.golf/",
                     f"https://{vanity}.play.teeitup.golf/"):
            try:
                page.goto(host, wait_until="networkidle", timeout=45000)
            except Exception:  # noqa: BLE001 — try the next host shape
                continue
            # let the SPA fire its tee-times call
            for _ in range(12):
                if got.get("alias"):
                    break
                page.wait_for_timeout(500)
            if got.get("alias"):
                break
    except Exception as e:  # noqa: BLE001
        got["error"] = f"{type(e).__name__}: {e}"
    finally:
        browser.close()
    return got


def main(argv=None) -> int:
    from playwright.sync_api import sync_playwright

    p = argparse.ArgumentParser(description="TeeItUp real-alias discovery")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--slugs", default="",
                   help="comma-separated course slugs (default: all teeitup "
                        "needs_ids in the registry)")
    a = p.parse_args(argv)

    reg = load_registry(a.registry)
    if a.slugs:
        want = {s.strip() for s in a.slugs.split(",") if s.strip()}
        courses = [c for c in reg if c["slug"] in want]
    else:
        courses = [c for c in reg if c["platform"] == "teeitup"
                   and c.get("status") == "needs_ids"]

    out: dict = {}
    with sync_playwright() as pw:
        for c in courses:
            v = _vanity(c.get("booking_url") or "") or \
                (c.get("ids") or {}).get("alias")
            if not v:
                out[c["slug"]] = {"error": "no vanity host in booking_url"}
                print(f"  {c['slug']:44s} NO VANITY", file=sys.stderr)
                continue
            rec = probe_one(pw, v)
            rec["name"] = c["name"]
            out[c["slug"]] = rec
            tag = (f"alias={rec.get('alias')} facility_id={rec.get('facility_id')}"
                   if rec.get("alias") else rec.get("error", "no alias captured"))
            print(f"  {c['slug']:44s} {tag}", file=sys.stderr)
            time.sleep(1)

    print("\n===== TEEITUP ALIAS DISCOVERY =====")
    print(json.dumps(out, indent=2))
    # EXTRA_IDS-ready block
    print("\n----- paste into build_registry.py EXTRA_IDS -----")
    for slug, rec in out.items():
        if rec.get("alias"):
            fid = rec.get("facility_id")
            body = f'"alias": "{rec["alias"]}"'
            if fid:
                body += f', "facility_id": "{fid}"'
            key = re.sub(r"\([^)]*\)", "", rec.get("name", "")).strip().lower()
            print(f'    "{key}": {{{body}}},')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
