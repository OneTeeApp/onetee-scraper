#!/usr/bin/env python3
"""One-time bootstrap seed: pull the current tee times from the live Cloudflare
worker and load them into Postgres via the local /ingest endpoint, so the new API
has real data to serve BEFORE the scraper backend swap. Lossy on purpose (it uses
the worker's already-deduped view) — the scrapers repopulate authoritatively once
they point at /ingest. Stdlib only."""
import json, urllib.request

WORKER = "https://onetee-api.damp-snow-8025.workers.dev"
API = "http://127.0.0.1:8080"
STATES = ["CO", "AZ", "FL", "MD", "UT", "VA"]


def env_val(path, key):
    try:
        for line in open(path):
            if line.startswith(key + "="):
                return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return ""


TOKEN = env_val("/root/onetee-api.env", "INGEST_TOKEN")


def fetch(url):
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.loads(r.read())


def post(rows):
    body = json.dumps({"rows": rows}).encode()
    req = urllib.request.Request(
        API + "/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


total = 0
for st in STATES:
    try:
        d = fetch(f"{WORKER}/api/tee-times?state={st}&limit=100000")
    except Exception as e:
        print("fetch fail", st, e)
        continue
    rows = d.get("tee_times", [])
    out = []
    for r in rows:
        out.append({
            "course_slug": r.get("course_slug"),
            "teetime": r.get("teetime"),
            # keep names clean: the worker already merged the sub-course label into
            # course_name, so store label='' to avoid the read API re-appending it.
            "course_label": "",
            "course_name": r.get("course_name", ""),
            "city": r.get("city"), "state": r.get("state"),
            "venue_id": r.get("venue_id") or r.get("course_slug"),
            "source_role": "primary",
            "platform": r.get("platform"), "holes": r.get("holes"),
            "open_spots": r.get("open_spots"), "price_min": r.get("price_min"),
            "price_max": r.get("price_max"), "currency": r.get("currency", "USD"),
            "booking_url": r.get("booking_url"), "simulated": r.get("simulated", 0) or 0,
            "active": 1,
            "first_seen_at": r.get("first_seen_at"),
            "last_seen_at": r.get("last_seen_at"),
        })
    for i in range(0, len(out), 2000):
        try:
            post(out[i:i + 2000])
        except Exception as e:
            print("post fail", st, e)
            break
    total += len(out)
    print(f"seeded {st}: {len(out)} rows (truncated={d.get('truncated')})")

print("TOTAL seeded:", total)
