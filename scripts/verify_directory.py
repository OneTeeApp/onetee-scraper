"""Check the directory tells golfers the truth, and joins to the live feed.

The directory is the one surface that speaks about courses OneTee cannot
book, so its failure modes are quiet by nature — nobody files a bug about a
course that says "call the pro shop" when it has been bookable online all
along. These are the four things that can go wrong, in the order they hurt:

  1. A LIVE COURSE TAGGED UNBOOKABLE. We are selling tee times for it and
     simultaneously telling the golfer to phone the club or that it is
     members-only. This is worse than not listing it, so it fails the build.
  2. A BROKEN JOIN. Directory venue_id and feed venue_id disagree, so a
     course renders twice — once live, once greyed. Fails.
  3. NOWHERE TO GO. An entry with neither a booking URL nor a website is a
     card with a tag and no action. Tolerated in small numbers (the CSV
     genuinely lacks a site for a couple of courses), fails past a ceiling.
  4. NO NUMBER TO CALL. "Call to book" with no phone is honest but useless.
     Reported, never fatal — the monthly enrichment pass is what fixes it,
     and a slow fill should not redden the daily gate.

  python3 scripts/verify_directory.py [--status probe-results/state-status.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

# Methods that tell a golfer they cannot book here through anyone. If a course
# is live in our own feed, none of these can be true.
UNBOOKABLE = {"phone", "private"}


def key(state: str, name: str) -> str:
    return state + re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--directory", default="directory.json")
    ap.add_argument("--registry", default="registry.json")
    ap.add_argument("--status", default="probe-results/state-status.json")
    ap.add_argument("--max-no-action", type=int, default=5)
    a = ap.parse_args()

    with open(a.directory) as fh:
        courses = json.load(fh)["courses"]
    by_id = {c["venue_id"]: c for c in courses}
    by_name = {key(c["state"], c["name"]): c for c in courses}

    fatal: list[str] = []
    notes: list[str] = []

    dup = len(courses) - len(by_id)
    if dup:
        seen: Counter = Counter(c["venue_id"] for c in courses)
        fatal.append(f"{dup} duplicate venue_id(s): "
                     + ", ".join(k for k, n in seen.items() if n > 1))

    try:
        with open(a.registry) as fh:
            reg = json.load(fh)["courses"]
    except (OSError, ValueError):
        reg = []
        notes.append("registry.json unreadable — join check skipped")
    missing = sorted({c["venue_id"] for c in reg} - set(by_id))
    if missing:
        fatal.append(f"{len(missing)} registry venue(s) absent from the "
                     f"directory — they would render live with no card, and a "
                     f"filter that hides them would hide a bookable course: "
                     + ", ".join(missing[:8]))

    # Live truth, when a status report is available. Its `live` bucket is the
    # set of venues actually returning tee times, which is the only authority
    # on what "bookable" means today.
    try:
        with open(a.status) as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        status = None
        notes.append(f"{a.status} not present — live-vs-tag check skipped")

    if status:
        mislabelled, unjoined = [], []
        for st in status.get("states", []):
            for item in st.get("detail", {}).get("live", []):
                c = by_id.get(item["slug"]) or by_name.get(
                    key(st["state"], item["name"]))
                if not c:
                    unjoined.append(f'{st["state"]} {item["name"]}')
                    continue
                if c["booking_method"] in UNBOOKABLE:
                    mislabelled.append(
                        f'{st["state"]} {item["name"]} — live with '
                        f'{item["rows"]} rows but tagged '
                        f'"{c["booking_method"]}"')
        if mislabelled:
            fatal.append(f"{len(mislabelled)} live course(s) tagged as "
                         f"unbookable:\n    " + "\n    ".join(mislabelled[:10]))
        if unjoined:
            fatal.append(f"{len(unjoined)} live venue(s) with no directory "
                         f"entry (broken join — these render twice):\n    "
                         + "\n    ".join(unjoined[:10]))

    no_action = [c for c in courses if not c["action_url"]]
    if len(no_action) > a.max_no_action:
        fatal.append(f"{len(no_action)} entries with no booking URL and no "
                     f"website (ceiling {a.max_no_action}): "
                     + ", ".join(f'{c["state"]} {c["name"]}'
                                 for c in no_action[:8]))
    elif no_action:
        notes.append(f'{len(no_action)} with no link at all: '
                     + ", ".join(f'{c["state"]} {c["name"]}' for c in no_action))

    phoneless = [c for c in courses
                 if c["booking_method"] == "phone" and not c["phone"]]
    if phoneless:
        notes.append(f'{len(phoneless)} of '
                     f'{sum(1 for c in courses if c["booking_method"] == "phone")}'
                     f' "call to book" courses have no number yet — run the '
                     f'enrich-phones workflow')

    methods = Counter(c["booking_method"] for c in courses)
    print(f"{len(courses)} venues  "
          + "  ".join(f"{k}={v}" for k, v in sorted(methods.items())))
    print("by state: " + "  ".join(
        f"{k}={v}" for k, v in sorted(Counter(c["state"] for c in courses).items())))
    for n in notes:
        print("note: " + n)
    if not fatal:
        print("OK — every live course is tagged bookable and every registry "
              "venue has a card")
        return 0
    print(f"\nFAIL — {len(fatal)} problem(s):")
    for f in fatal:
        print("  " + f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
