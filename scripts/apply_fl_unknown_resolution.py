"""Apply probe-results/fl-unknown-resolution.json to florida_golf_courses_booking.csv.

The Florida research file landed with 179 rows whose booking method was
unknown — 27% of the state, rendering as "Booking method unconfirmed" on the
site. Six regional research passes resolved them; this applies the verdicts.

The evidence rule that shapes this file: a platform + booking URL is written
ONLY where the booking link was published on the course's own site (or its
operator's). Several researchers "confirmed" TeeItUp by observing that
<name>.book.teeitup.com resolves — that is exactly the trap build_registry.py's
Golden Hills note documents, because a nonsense subdomain serves the same SPA
shell and a wrong alias publishes another club's tee sheet under our name.
Those rows are written as `online_nolink`: Online Booking=yes with no platform,
so the directory sends the golfer to the course's own site and the registry
never mints a booking source we cannot stand behind.

Verdict handling:
  online        -> Online Booking=yes, platform + booking URL written
  online_nolink -> Online Booking=yes, no platform (course site is the target)
  phone         -> Online Booking=no
  private       -> Type=Private, Online Booking=private
  closed        -> Type=Closed, Online Booking=no
  unresolved    -> left unknown; the finding is appended to Notes so the next
                   pass starts warm instead of repeating the dead ends

Every write is guarded on the row existing exactly once, because a silent
no-match would look identical to a successful run.
"""
import csv
import json
import sys
from collections import Counter

CSV = "florida_golf_courses_booking.csv"
SRC = "probe-results/fl-unknown-resolution.json"


def main() -> int:
    data = json.load(open(SRC))
    verdicts = {k: v for k, v in data["verdicts"].items() if not k.startswith("_")}
    rows = list(csv.DictReader(open(CSV)))
    # Two distinct Florida courses can share a name — "Pinecrest Golf Club" is
    # both a closed Largo muni and an open Avon Park club — so a verdict key may
    # be qualified as "Course Name@City" to name exactly one of them.
    by_name = {}
    for r in rows:
        by_name.setdefault(r["Course Name"], []).append(r)
        by_name.setdefault(f"{r['Course Name']}@{r['City']}", []).append(r)

    stats, missing, ambiguous = Counter(), [], []
    for name, v in verdicts.items():
        hits = by_name.get(name, [])
        if not hits:
            missing.append(name)
            continue
        if len(hits) > 1:
            ambiguous.append(name)
            continue
        row = hits[0]
        if row["Online Booking"] != "unknown":
            # Only the unknown cohort is in scope; anything else means the CSV
            # moved under us and the verdict may no longer describe this row.
            stats["skipped_not_unknown"] += 1
            continue

        if isinstance(v, str):                      # unresolved
            row["Notes"] = f"{row['Notes']} | Research 2026-07-29: {v}".strip(" |")
            stats["unresolved"] += 1
            continue

        kind = v[0]
        if kind in ("online", "online_nolink"):
            plat, url, site, note = v[1], v[2], v[3], v[4]
            row["Online Booking"] = "yes"
            row["Booking Platform"] = plat
            row["Booking URL"] = url
            if site:
                row["Website"] = site
            row["Confidence"] = "high" if kind == "online" else "medium"
            row["Notes"] = f"{row['Notes']} | Research 2026-07-29: {note}".strip(" |")
            stats[kind] += 1
        elif kind == "phone":
            site, note = v[1], v[2]
            row["Online Booking"] = "no"
            if site:
                row["Website"] = site
            row["Notes"] = f"{row['Notes']} | Research 2026-07-29: {note}".strip(" |")
            stats["phone"] += 1
        elif kind == "private":
            site, note = v[1], v[2]
            row["Type"] = "Private"
            row["Online Booking"] = "private"
            if site:
                row["Website"] = site
            row["Notes"] = f"{row['Notes']} | Research 2026-07-29: {note}".strip(" |")
            stats["private"] += 1
        elif kind == "closed":
            row["Type"] = "Closed"
            row["Online Booking"] = "no"
            row["Notes"] = f"{row['Notes']} | Research 2026-07-29: {v[1]}".strip(" |")
            stats["closed"] += 1
        else:
            raise SystemExit(f"unknown verdict kind {kind!r} for {name}")

    for name, meta in data.get("new_courses_found", {}).items():
        if name in by_name:
            stats["new_skipped_present"] += 1
            continue
        city, typ, site, phone, note = meta
        rows.append({
            "Course Name": name, "Display Name": "", "City": city, "Zip": "",
            "Type": typ, "Website": site, "Booking Platform": "", "Booking URL": "",
            "Online Booking": "unknown", "Confidence": "medium",
            "Notes": f"Added 2026-07-29 from the NE Florida coverage audit: {note}"
                     f" | Region: Northeast FL (Jacksonville) | Phone: {phone}",
        })
        stats["new_rows"] += 1

    if missing or ambiguous:
        print("ABORT — verdicts that do not map to exactly one CSV row:", file=sys.stderr)
        for n in missing:
            print(f"  no match: {n}", file=sys.stderr)
        for n in ambiguous:
            print(f"  ambiguous: {n}", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r["City"], r["Course Name"]))
    with open(CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"{CSV}: {len(rows)} rows")
    for k, n in sorted(stats.items()):
        print(f"  {k:24s} {n}")
    still = sum(1 for r in rows if r["Online Booking"] == "unknown")
    print(f"  still unknown            {still}  (was 179)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
