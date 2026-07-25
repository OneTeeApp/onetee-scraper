"""Build directory.json — EVERY course in every state, not just the scrapable ones.

WHY THIS EXISTS
---------------
registry.json is the scrape list: build_registry drops any row without online
booking and a recognised platform, which is correct for a scraper and wrong for
a golfer. A golfer searching "Colorado Springs" should find The Broadmoor and
learn it is phone-only, rather than conclude OneTee has never heard of it.

So this emits the OTHER view of the same CSVs: one entry per venue, all of them,
each carrying how a person actually books it and where to go to do that.

  booking_method   what the golfer does
  --------------   ------------------------------------------------------
  online           books online — link goes to the real booking page
  phone            calls the pro shop — link goes to the course website
  private          members only; nothing to book. Still listed, because
                   "not listed" and "cannot be booked" look identical to a
                   golfer, and only one of them is true.
  unknown          the directory could not establish either way

Deliberately NOT recorded here: whether OneTee currently serves tee times for
the course. That is a live fact, it changes hourly, and the widget already
knows it from the tee-time feed. Baking it in would guarantee a stale badge.

venue_id matches registry.json's for every course that has one, so the widget
can join the directory against the tee-time feed on a single key.

  python3 build_directory.py
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, OrderedDict

from build_registry import SOURCES, slugify

OUT = "directory.json"

# Type values that mean a golfer cannot buy a tee time at any price.
PRIVATE_TYPES = {"private", "military"}

# Phone numbers already captured in the directory's Notes column. Only a
# handful today; scripts/enrich_phones.py fills in the rest.
PHONE_RE = re.compile(r"\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})")


def find_phone(*texts: str) -> str:
    for t in texts:
        m = PHONE_RE.search(t or "")
        if m:
            return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return ""


def method_for(rows: list[dict]) -> str:
    """How does a golfer book this venue? Best case across its rows wins."""
    booking = {(r.get("Online Booking") or "").strip().lower() for r in rows}
    types = {(r.get("Type") or "").strip().lower() for r in rows}
    if "yes" in booking:
        return "online"
    # Private is checked AFTER online: a private club that still publishes a
    # public tee sheet is bookable, and the tee sheet is the fact that matters.
    if types and types <= PRIVATE_TYPES:
        return "private"
    if "no" in booking:
        return "phone"
    return "unknown"


# What the site should say. Kept here rather than in the widget so the wording
# is versioned and one edit changes every surface that renders it.
LABEL = {
    "online": "Book online",
    "phone": "Call to book",
    "private": "Private club — members only",
    "unknown": "Booking method unconfirmed",
}
BLURB = {
    "online": "This course takes online bookings on its own system.",
    "phone": "No online tee sheet — call the pro shop to reserve.",
    "private": "Members and guests only. No public tee times.",
    "unknown": "We could not confirm how this course takes bookings. "
               "Check its website.",
}


def clean_url(u: str) -> str:
    u = (u or "").strip()
    if not u or u.lower() in {"n/a", "none", "-"}:
        return ""
    return u if u.startswith(("http://", "https://")) else f"https://{u}"


def main() -> None:
    # venue_id must agree with registry.json wherever the registry has an
    # opinion, so the widget can join on one key. Seed from it rather than
    # re-deriving the collision rule and hoping the two stay in step.
    reg_venue: dict[tuple, str] = {}
    try:
        with open("registry.json") as fh:
            for c in json.load(fh)["courses"]:
                reg_venue[(c["state"], slugify(c["name"]))] = c["venue_id"]
    except (OSError, ValueError, KeyError):
        pass

    phones: dict[str, str] = {}
    if os.path.exists("local/phones.json"):
        with open("local/phones.json") as fh:
            phones = json.load(fh)

    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for src, state in SOURCES:
        try:
            fh = open(src)
        except FileNotFoundError:
            continue
        with fh:
            for row in csv.DictReader(fh):
                groups.setdefault((state, slugify(row["Course Name"])),
                                  []).append(row)

    taken = set(reg_venue.values())
    out = []
    for (state, vb), rows in groups.items():
        vid = reg_venue.get((state, vb))
        if not vid:
            vid = vb if vb not in taken else f"{vb}-{state.lower()}"
            while vid in taken:
                vid += "-x"
        taken.add(vid)

        method = method_for(rows)
        # Prefer a row that actually books: its Booking URL is the useful link.
        best = sorted(rows, key=lambda r: (r.get("Online Booking") != "yes",
                                           not (r.get("Booking URL") or "").strip()))[0]
        website = clean_url(best.get("Website", ""))
        booking = clean_url(best.get("Booking URL", ""))
        out.append({
            "venue_id": vid,
            "name": best["Course Name"],
            "city": best.get("City", ""),
            "state": state,
            "zip": best.get("Zip", ""),
            "type": best.get("Type", ""),
            "booking_method": method,
            "label": LABEL[method],
            "blurb": BLURB[method],
            "website": website,
            # Where the button goes. Online courses get the real booking page;
            # everyone else gets the course site, which is the only place a
            # golfer can act. Never empty unless we have neither, which the
            # verifier flags.
            "action_url": booking or website,
            "phone": phones.get(vid) or find_phone(*(r.get("Notes", "")
                                                     for r in rows)),
            "platforms": sorted({(r.get("Booking Platform") or "").strip()
                                 for r in rows if (r.get("Booking Platform")
                                                   or "").strip()}),
        })

    out.sort(key=lambda c: (c["state"], c["name"]))
    with open(OUT, "w") as fh:
        json.dump({"generated_from": [s for s, _ in SOURCES],
                   "courses": out}, fh, indent=1)

    print(f"wrote {OUT}: {len(out)} venues")
    print("by state:", dict(Counter(c["state"] for c in out)))
    print("by method:", dict(Counter(c["booking_method"] for c in out)))
    print("with phone:", sum(1 for c in out if c["phone"]))
    print("no action_url:", sum(1 for c in out if not c["action_url"]))


if __name__ == "__main__":
    main()
