"""Post aggregated tee times to OneTee (www.oneteeapp.com).

OneTee currently has no public API (the site is a coming-soon page as of
July 2026), so this module is a ready-to-wire client: point it at your ingest
endpoint when the backend exists. Design assumptions, all overridable:

* POST <ONETEE_API_URL>  with JSON body {"source": ..., "date": ...,
  "tee_times": [...]}, batched.
* Bearer auth via ONETEE_API_KEY.

Usage:
    export ONETEE_API_URL="https://api.oneteeapp.com/v1/tee-times/bulk"
    export ONETEE_API_KEY="..."
    python -m scraper.onetee --data output/tee_times.json
    # or after any aggregate run:
    python -m scraper.aggregate --platforms teeitup && python -m scraper.onetee

Until the API exists, `--dry-run` prints what would be sent, and
`--export-csv` writes the same records as CSV for manual import
(e.g. into Squarespace, Airtable, or a database).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sys
import time

import requests

BATCH = 500


def post(doc: dict, url: str, api_key: str, dry_run: bool = False) -> int:
    """POST tee times in batches; returns number of records sent."""
    times = doc["tee_times"]
    sent = 0
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {api_key}",
                      "Content-Type": "application/json",
                      "User-Agent": "onetee-aggregator/0.1"})
    for i in range(0, len(times), BATCH):
        payload = {
            "source": "colorado-aggregator",
            "generated_at": doc["generated_at"],
            "date": doc["date"],
            "simulated": doc.get("simulated", False),
            "batch": i // BATCH,
            "tee_times": times[i:i + BATCH],
        }
        if dry_run:
            print(f"[dry-run] would POST batch {i//BATCH}: "
                  f"{len(payload['tee_times'])} records to {url}")
            sent += len(payload["tee_times"])
            continue
        for attempt in range(3):
            r = s.post(url, json=payload, timeout=30)
            if r.status_code < 300:
                sent += len(payload["tee_times"])
                break
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
    return sent


def export_csv(doc: dict, out_path: str) -> None:
    fields = ["course_slug", "course_name", "city", "platform", "teetime",
              "holes", "open_spots", "price_min", "price_max", "currency",
              "booking_url", "simulated"]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in doc["tee_times"]:
            row = {k: t.get(k) for k in fields}
            row["holes"] = "/".join(map(str, t.get("holes") or []))
            w.writerow(row)
    print(f"wrote {out_path} ({len(doc['tee_times'])} rows)")


def main() -> int:
    p = argparse.ArgumentParser(description="Send tee times to OneTee")
    p.add_argument("--data", default="output/tee_times.json")
    p.add_argument("--url", default=os.environ.get("ONETEE_API_URL"))
    p.add_argument("--api-key", default=os.environ.get("ONETEE_API_KEY", ""))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--export-csv", metavar="PATH",
                   help="also write records as CSV (works without an API)")
    a = p.parse_args()

    doc = json.loads(pathlib.Path(a.data).read_text())
    if a.export_csv:
        export_csv(doc, a.export_csv)
    if not a.url:
        print("No ONETEE_API_URL set — skipping POST. (OneTee has no public "
              "API yet; use --export-csv meanwhile.)", file=sys.stderr)
        return 0
    n = post(doc, a.url, a.api_key, a.dry_run)
    print(f"{'[dry-run] ' if a.dry_run else ''}sent {n} tee times to {a.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
