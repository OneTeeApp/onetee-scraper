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
WORKER_DIR = "worker"

# Type values that mean a golfer cannot buy a tee time at any price.
PRIVATE_TYPES = {"private", "military"}

# A course that has shut down is not a booking method, it is an absence, and
# every label this file can emit would be a lie about it — "Call to book" most
# of all, since directories still carry a number for Saint Andrews at
# Westcliffe nine years after it closed and a golfer dialling it reaches
# whoever has that line now.
#
# The row stays in the CSV rather than being deleted, carrying the evidence in
# its Notes. Deleting it would lose the finding, and the next refresh from an
# upstream course list would quietly put the course back.
CLOSED_TYPES = {"closed"}

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


# Hosts that are somebody else's front door. A row reading
# "https://www.facebook.com" means "this course only exists on Facebook and
# nobody wrote down which page" — thirteen Colorado venues said exactly that.
# Rendered as the course's website it becomes a Book/Visit button that drops a
# golfer on facebook.com's homepage, which is worse than no button: it looks
# like we have the course covered and it answers nothing.
#
# Only the bare host is rejected. A real page on any of these — a course's
# actual Facebook page, its GolfPass listing — has a path and survives.
NOT_A_WEBSITE = {"facebook.com", "www.facebook.com", "m.facebook.com",
                 "instagram.com", "www.instagram.com",
                 "golfpass.com", "www.golfpass.com",
                 "colorado.com", "www.colorado.com",
                 "coloradoavidgolfer.com", "www.coloradoavidgolfer.com"}


def clean_url(u: str) -> str:
    u = (u or "").strip()
    if not u or u.lower() in {"n/a", "none", "-"}:
        return ""
    u = u if u.startswith(("http://", "https://")) else f"https://{u}"
    rest = u.split("://", 1)[1]
    host, _, path = rest.partition("/")
    if host.lower() in NOT_A_WEBSITE and not path.strip("?#"):
        return ""
    return u


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

    # Hand-verified numbers outrank the crawl. enrich_phones.py rewrites
    # local/phones.json every month from whatever tel: link a course site
    # exposes, and on a corporately-managed course that is a switchboard in
    # another state. A number a human checked should not be quietly replaced by
    # one scraped off a management company's page, so it lives in a file the
    # crawler never writes and is read first.
    #
    # An entry with phone=null is a deliberate hold — a course that is closed,
    # or whose listings disagree — and it must beat both the crawl and the
    # Notes fallback. `curated.get(vid) or phones.get(vid)` would skip right
    # past a null and publish the very number the hold exists to suppress, so
    # membership is tested, not truthiness.
    curated: dict[str, str | None] = {}
    if os.path.exists("local/phones.curated.json"):
        with open("local/phones.curated.json") as fh:
            for vid, e in (json.load(fh).get("courses") or {}).items():
                curated[vid] = e.get("phone") if isinstance(e, dict) else e

    def phone_for(vid: str, rows: list) -> str:
        if vid in curated:
            return curated[vid] or ""
        return phones.get(vid) or find_phone(*(r.get("Notes", "") for r in rows))

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
    closed = []
    for (state, vb), rows in groups.items():
        if all((r.get("Type") or "").strip().lower() in CLOSED_TYPES
               for r in rows):
            closed.append(rows[0]["Course Name"])
            continue
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
            "phone": phone_for(vid, rows),
            "platforms": sorted({(r.get("Booking Platform") or "").strip()
                                 for r in rows if (r.get("Booking Platform")
                                                   or "").strip()}),
        })

    out.sort(key=lambda c: (c["state"], c["name"]))
    doc = {"generated_from": [s for s, _ in SOURCES], "courses": out}
    with open(OUT, "w") as fh:
        json.dump(doc, fh, indent=1)

    # The Worker serves this list, and it is small and changes only when a
    # state CSV changes — so it ships INSIDE the bundle rather than becoming a
    # D1 table nobody remembers to migrate. Emitted as a .js module rather than
    # imported as .json so no bundler has to be trusted with a JSON loader.
    os.makedirs(WORKER_DIR, exist_ok=True)
    with open(os.path.join(WORKER_DIR, "directory.gen.js"), "w") as fh:
        fh.write("// GENERATED by build_directory.py — do not edit.\n"
                 "// Regenerate with: python3 build_directory.py\n"
                 "export default ")
        json.dump(doc, fh, separators=(",", ":"))
        fh.write(";\n")

    print(f"wrote {OUT}: {len(out)} venues")
    if closed:
        # Named, not just counted. A venue vanishing from the directory is the
        # kind of change that should never happen quietly.
        print(f"excluded {len(closed)} closed: {', '.join(sorted(closed))}")
    print("by state:", dict(Counter(c["state"] for c in out)))
    print("by method:", dict(Counter(c["booking_method"] for c in out)))
    print("with phone:", sum(1 for c in out if c["phone"]))
    print("no action_url:", sum(1 for c in out if not c["action_url"]))


if __name__ == "__main__":
    main()
