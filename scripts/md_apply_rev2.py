#!/usr/bin/env python3
"""Apply Brian's 2026-07-30 revision of the Maryland registry workbook.

Adapting a hand-audited workbook is NOT a straight overwrite. Three rules
decide every field, in this order:

  1. MEASURED BEATS RESEARCHED. Where the workbook's desk research disagrees
     with something this repo has measured against the live engine, the
     measurement wins and the row records both. Six rows are overridden on
     that basis (five Classic Five pins + Mountain Branch).
  2. THE URL WINS OVER THE LABEL. A platform label is a human's summary; the
     URL is what extract_ids() actually parses. `sc.cps.golf/<Tenant>` is a
     Club Prophet SHARED host whose tenant lives in the path, which our
     `<tenant>.cps.golf` regex mis-reads as tenant "sc" — so that row ships
     as other:cps-shared (the Fleming Island precedent), not clubprophet.
  3. NO TWO ROWS SHIP `yes` ON THE SAME BOOKING URL WITHOUT A PER-COURSE ID.
     Queenstown Harbor's two courses share one Chronogolf club, so each pins
     its own course_id (verified live: River 8665, Lakes 8664). The nine
     already-held rows keep their holds for exactly this reason.

Run from the repo root. Writes maryland_golf_courses_booking.csv in place.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import openpyxl

XLSX = sys.argv[1] if len(sys.argv) > 1 else (
    "/root/.claude/uploads/3abf641b-d3cc-5384-ad48-505046099df8/"
    "fe362a38-Maryland_Golf_Courses_Booking_Directory.xlsx")
CSV_PATH = Path("maryland_golf_courses_booking.csv")
REV = "Brian's MD registry rev 2026-07-30"

# Workbook platform label -> our platform slug. `other:*` means "engine known,
# no adapter": the row still earns a Book-online card, it just is not scraped.
LABEL2SLUG = {
    "Agilysys rGuest": "rguest",
    "Chronogolf (Lightspeed)": "chronogolf",
    "Club Prophet (CPS)": "clubprophet",
    "Clubessential": "other:clubessential",
    "Course website (embedded widget)": "other:square",
    "EZLinks (TeeOff)": "ezlinks",
    "Eagle Club Systems": "other:eagleclubsystems",
    "EasyTeeGolf": "other:easyteegolf",
    "ForeUp": "foreup",
    "GolfBack": "other:golfback",
    "GolfRev": "other:golfrev",
    "None (phone only)": "",
    "TeeItUp (Golf Genius)": "teeitup",
    "Teesnap": "teesnap",
    "Unconfirmed": "",
}
CONF = {"VERIFIED": "high", "UPDATED": "high", "NO_ONLINE": "high",
        "UNVERIFIED": "low"}

# ---------------------------------------------------------------- overrides --
# Each entry replaces the workbook's booking columns and appends a clause to
# Notes saying why. "why" is the evidence, not an opinion.

def _pin(schedule: str, label: str) -> dict:
    return {
        "url": f"https://foreupsoftware.com/index.php/booking/20751/{schedule}#/teetimes",
        "why": (f"Own foreUp schedule {schedule} kept over the revision's shared "
                f"portal URL (20751/5934, which is Pine Ridge's sheet): the five "
                f"Classic Five venues are serving five distinct inventories live "
                f"today, so the pins are load-bearing"),
    }


OVERRIDES: dict[str, dict] = {
    # 1. Classic Five. The revision collapses all five to the shared portal.
    #    /api/courses?state=MD measured five different slot counts on 2026-07-30
    #    (Carroll 536, Clifton 409, Forest Park 357, Mount Pleasant 267,
    #    Pine Ridge 165), which only happens because each row pins its own
    #    schedule. Collapsing them would publish Pine Ridge's sheet five times.
    "Carroll Park Golf Course": _pin("6231", "Carroll Park"),
    "Clifton Park Golf Course": _pin("6229", "Clifton Park"),
    "Forest Park Golf Course": _pin("6170", "Forest Park"),
    "Mount Pleasant Golf Course": _pin("5956", "Mount Pleasant"),
    "Pine Ridge Golf Course": _pin("5934", "Pine Ridge"),

    # 2. Mountain Branch. The revision downgrades it to Unconfirmed and asks
    #    whether the course is still trading. It is: GolfNow facility 6254 was
    #    serving 181 upcoming tee times on 2026-07-30. A mismatched certificate
    #    on the club's own domain is not evidence of a closed golf course.
    "Mountain Branch Golf Club": {
        "platform": "golfnow",
        "url": "https://www.golfnow.com/tee-times/facility/6254-mountain-branch-golf-course/search",
        "online": "yes", "conf": "high",
        "why": ("Kept on GolfNow against the revision's Unconfirmed downgrade: "
                "facility 6254 was serving 181 upcoming tee times when measured "
                "2026-07-30, so the course is trading and bookable"),
    },

    # 3. Bulle Rock. bullerockgc.com is the right club site (the revision is
    #    correct there) and 19835/2598 is the right golfer-facing portal, but
    #    that sheet is not anonymously readable: foreUp answered 400 "Booking
    #    Class ID required" bare and 401 on all three booking classes its own
    #    page advertises (2557/2558/2560). GolfNow facility 866 was serving 107
    #    tee times the same day, so the scrape stays where the data is.
    "Bulle Rock Golf Course": {
        "platform": "golfnow",
        "url": "https://www.golfnow.com/tee-times/facility/866-bulle-rock-golf-club/search",
        "online": "yes", "conf": "high",
        "why": ("Club's own foreUp portal is 19835/2598 but it 401s to anonymous "
                "callers on every booking class its page lists (2557/2558/2560) "
                "and 400s without one, measured 2026-07-30; GolfNow facility 866 "
                "was serving 107 tee times the same day, so the scrape stays "
                "there and the foreUp id is recorded for a later pass"),
    },

    # 4. Swan Point. The EZLinks portal is almost certainly right, but ezlinks
    #    is browser-owned and unverifiable from here, and GolfNow facility 952
    #    is measured live at 174 tee times. Do not trade measured inventory for
    #    an unmeasured platform; carry the portal as the next lead.
    "Swan Point Yacht & Country Club": {
        "platform": "golfnow",
        "url": "https://www.golfnow.com/tee-times/facility/952-swan-point-yacht-country-club/search",
        "online": "yes", "conf": "high",
        "why": ("EZLinks portal swanpointccpp.ezlinksgolf.com recorded as the "
                "next lead, but GolfNow facility 952 was serving 174 tee times "
                "on 2026-07-30 and ezlinks cannot be verified without the "
                "browser fetcher, so the measured source stays primary"),
    },

    # 5. Westminster National. Both sources measured live, so take the native
    #    one as primary (real prices, own booking link) and keep the GolfNow row
    #    as the deduped overflow — the same two-row shape 16 AZ/CO venues use.
    #    Note the facility integer is 11304 on BOTH, which is the tell that
    #    GolfNow and TeeItUp share one facility namespace.
    "Westminster National Golf Course": {
        "platform": "teeitup",
        "url": "https://westminster-national-golf-club.book.teeitup.com/",
        "online": "yes", "conf": "high",
        "why": ("TeeItUp alias westminster-national-golf-club resolves to "
                "facility 11304 and served 13-58 tee times/day across four dates "
                "measured 2026-07-30; the GolfNow row for the same facility id is "
                "kept as the deduped overflow source"),
    },

    # 6. Musket Ridge. Right URL, wrong label for our parser (see rule 2).
    "Musket Ridge Golf Club": {
        "platform": "other:cps-shared",
        "url": "https://sc.cps.golf/MusketRidgeWebstore/",
        "online": "yes", "conf": "high",
        "why": ("Club Prophet on the SHARED sc.cps.golf host with a path tenant; "
                "our clubprophet regex expects <tenant>.cps.golf and would "
                "mis-extract tenant=sc, so this ships as other:cps-shared (the "
                "Fleming Island precedent) — golfers get a working link, the "
                "scraper does not guess"),
    },

    # 7-9. Three TeeItUp tenants that exist and publish nothing. The aliases are
    #    real (a bogus alias 404s; these return HTTP 200) and each resolves to
    #    one correctly-named facility, but /v2/tee-times returned zero slots on
    #    every date tried with the facility pinned, and the booking page itself
    #    says "There are no tee times available". Shipping these as
    #    teeitup/yes would manufacture three silent courses in peak season.
    "Beach Club Golf Links": {
        "platform": "", "url": "", "online": "", "conf": "low",
        "why": ("TeeItUp alias beach-club-golf-links is real and resolves to "
                "facility 1209 under the right name, but /v2/tee-times returned "
                "0 slots on 2026-07-30/31 and 08-02/06 with the facility pinned "
                "and the booking page itself reports no tee times available — "
                "held rather than shipped as a silent course; alias 1209 recorded"),
    },
    "Eagle's Landing Golf Course": {
        "platform": "", "url": "", "online": "", "conf": "low",
        "why": ("TeeItUp alias eagles-landing-golf-course resolves to facility "
                "1210 under the right name but published 0 slots on all four "
                "dates measured 2026-07-30 — held rather than shipped silent; "
                "Town of Ocean City course, worth a phone check for the real "
                "engine, alias 1210 recorded"),
    },
    "Wood Creek Golf Links": {
        "platform": "", "url": "", "online": "", "conf": "low",
        "why": ("TeeItUp alias wood-creek-golf-links resolves to facility 9638 "
                "under the right name but published 0 slots on all four dates "
                "measured 2026-07-30 — held rather than shipped silent; "
                "alias 9638 recorded"),
    },

    # 10-11. Queenstown Harbor. One Chronogolf club, two bookable courses.
    #    Verified live 2026-07-30 on club 7597: River = course 8665 (70 times),
    #    Lakes = course 8664 (74 times); the club also lists two out-of-state
    #    courses with online booking disabled. Each venue pins its own id in
    #    build_registry.EXTRA_IDS, exactly as Turf Valley's two courses do.
    "Queenstown Harbor - River Course": {
        "platform": "chronogolf",
        "url": "https://www.chronogolf.com/club/queenstown-harbor-golf-links",
        "online": "yes", "conf": "high",
        "why": ("Chronogolf club 7597; course_id 8665 pinned in EXTRA_IDS and "
                "verified live 2026-07-30 (70 times, from $155). Without the pin "
                "both Queenstown venues would publish each other's sheet"),
    },
    "Queenstown Harbor - Lakes Course": {
        "platform": "chronogolf",
        "url": "https://www.chronogolf.com/club/queenstown-harbor-golf-links",
        "online": "yes", "conf": "high",
        "why": ("Chronogolf club 7597; course_id 8664 pinned in EXTRA_IDS and "
                "verified live 2026-07-30 (74 times). Without the pin both "
                "Queenstown venues would publish each other's sheet"),
    },
    "University of Maryland Golf Course": {
        "platform": "chronogolf",
        "url": "https://www.chronogolf.com/club/university-of-maryland-golf-club",
        "online": "yes", "conf": "high",
        "why": ("Chronogolf club 7630 verified live 2026-07-30 (82 times, from "
                "$105); course_id 8701 pinned in EXTRA_IDS so the club's "
                "non-bookable UMD Sim Room sheet can never leak in"),
    },
    "River Marsh Golf Club": {
        "platform": "teeitup",
        "url": "https://river-marsh-simulator.book.teeitup.com/?course=946",
        "online": "yes", "conf": "high",
        "why": ("Alias is named for the simulator but resolves to exactly one "
                "facility, 946 'River Marsh GC at The Hyatt Chesapeake' — the "
                "outdoor 18. Verified live 2026-07-30: 7 times/day on four "
                "dates, which is the resort's real public allocation"),
    },
    # Verified-live chronogolf additions that need no pin (single bookable
    # course each) but do deserve the measurement on the record.
    "Patriots Glen Golf Club": {
        "platform": "chronogolf",
        "url": "https://www.chronogolf.com/club/the-club-at-patriots-glen",
        "online": "yes", "conf": "high",
        "why": "Chronogolf club 7617 / course 8687 verified live 2026-07-30 (65 times, from $55)",
    },
    "The Timbers at Troy Golf Club": {
        "platform": "chronogolf",
        "url": "https://www.chronogolf.com/club/the-timbers-at-troy",
        "online": "yes", "conf": "high",
        "why": ("Chronogolf club 7622 / course 8692 verified live 2026-07-30 "
                "(84 times, from $66); the noteefy host is a waitlist, not a tee sheet"),
    },
    # Measured-live TeeItUp upgrades off the dead unclaimed-Chronogolf listings.
    "Ocean Pines Golf Club": {
        "platform": "teeitup",
        "url": "https://ocean-pines-golf-and-country-club.book.teeitup.com/",
        "online": "yes", "conf": "high",
        "why": "TeeItUp facility 883 verified live 2026-07-30 (11-60 times/day across four dates)",
    },
    "Ocean Resorts Golf Club": {
        "platform": "teeitup",
        "url": "https://ocean-resorts-golf-club.book.teeitup.com/",
        "online": "yes", "conf": "high",
        "why": "TeeItUp facility 6939 verified live 2026-07-30 (24-63 times/day across four dates)",
    },
    "River Run Golf Club": {
        "platform": "teeitup",
        "url": "https://river-run-golf-club.book.teeitup.com/",
        "online": "yes", "conf": "high",
        "why": ("TeeItUp facility 950 verified live 2026-07-30 (62-82 times/day). "
                "Confirms the Chronogolf hold: club 19908's only course, 28443, "
                "has online booking disabled"),
    },
    "Maryland National Golf Club": {
        "platform": "teeitup",
        "url": "https://maryland-national-golf-club-2.book.teeitup.com/",
        "online": "yes", "conf": "high",
        "why": "TeeItUp facility 7184 verified live 2026-07-30 (7-47 times/day across four dates)",
    },
}

# Rows already held out of the registry pending a per-course id. The revision
# re-confirms all nine on the same shared URLs, which is exactly the shape that
# cannot ship: five Baltimore County venues on one foreUp portal, two Prince
# George's on one TeeItUp alias, two Ocean City on one CPS tenant. Shipping any
# of them `yes` would publish one sheet under several venue names.
HELD = {
    "Rocky Point Golf Course", "Fox Hollow Golf Course", "Greystone Golf Course",
    "Diamond Ridge Golf Course", "The Woodlands Golf Course",
    "Enterprise Golf Course", "Henson Creek Golf Course",
    "Ocean City Golf Club - Newport Bay", "Ocean City Golf Club - Seaside",
}
HELD_WHY = ("still held: the revision re-confirms this shared portal but gives "
            "no per-course id, and two venues on one URL would publish one "
            "sheet twice")

# Courses the revision dropped. Researched 2026-07-30: all seven are closed, so
# they stay in the CSV as the audit record with Type: Closed, which keeps them
# out of both the registry and the directory. Deleting the rows would lose the
# finding; leaving them Public would put a dead course on the site behind a
# "Call to book" number, which is the Saint Andrews at Westcliffe mistake.
CLOSED = {
    "Chesapeake Bay Golf Club - North East":
        "closed; the North East course is gone and the site is under a 726-unit "
        "residential proposal (Cecil Whig, 2023). Rising Sun is the surviving course",
    "Choptank River Golf & Events":
        "closed; golfmaryland.com lists it Closed and TripAdvisor reports it "
        "permanently closed, newest review 2017",
    "Deer Run Golf Club":
        "closed as a golf course in 2015-16; the land became Deer Run Jeep Golf, "
        "which itself drew a county stop-work order",
    "Frederick Golf Club":
        "closed; no website, Yelp closed, newest review 2015. Softer evidence "
        "than the other six - directory listings only, no closure record",
    "Horse Bridge Golf Course":
        "closed for business in 2019 per GolfPass; the disc-golf course on the "
        "same property is also permanently closed",
    "Nassawango Country Club":
        "closed; the land was absorbed into a Maryland state park, expanding it "
        "by 212 acres",
    "River House at the Easton Club":
        "closed; golf suspended 2015, foreclosed and sold at auction 2016, now "
        "an events venue (Star Democrat, 2021)",
}

# A second source row for one venue: native primary + GolfNow overflow. Both
# measured live on 2026-07-30.
EXTRA_ROWS = [{
    "Course Name": "Westminster National Golf Course",
    "Display Name": "",
    "City": "Westminster", "Zip": "", "Type": "Public",
    "Website": "https://www.westminsternationalgolf.com/",
    "Booking Platform": "golfnow",
    "Booking URL": "https://www.golfnow.com/tee-times/facility/11304-westminster-national-golf-club/search",
    "Online Booking": "yes", "Confidence": "high",
    "Notes": ("GolfNow overflow source for the TeeItUp primary row above - same "
              "facility integer 11304 on both, which is how GolfNow and TeeItUp "
              "share a namespace. Measured 72 upcoming times 2026-07-30; the "
              "Worker dedupes any slot both sources list. | Central & Northern "
              f"MD region | From {REV}"),
}]

FIELDS = ["Course Name", "Display Name", "City", "Zip", "Type", "Website",
          "Booking Platform", "Booking URL", "Online Booking", "Confidence",
          "Notes"]


def main() -> int:
    ws = openpyxl.load_workbook(XLSX)["Courses"]
    rows = list(ws.values)
    hdr = list(rows[0])
    new: dict[str, dict] = {}
    order: list[str] = []
    for r in rows[1:]:
        d = {k: ("" if v is None else str(v).strip()) for k, v in zip(hdr, r)}
        if not d.get("Course Name"):
            continue
        new[d["Course Name"]] = d
        order.append(d["Course Name"])

    old = {d["Course Name"]: d for d in csv.DictReader(CSV_PATH.open())}

    unknown = sorted({d["Booking Platform"] for d in new.values()} - set(LABEL2SLUG))
    if unknown:
        print("ABORT: unmapped platform labels:", unknown)
        return 1

    out: list[dict] = []
    notes_of = {}
    for name in order:
        d, o = new[name], old.get(name, {})
        plat = LABEL2SLUG[d["Booking Platform"]]
        url = d["Booking URL (scrape target)"]
        online = "yes" if plat and url else ("no" if d["Verification"] == "NO_ONLINE" else "")
        conf = CONF.get(d["Verification"], "low")
        clauses = [c for c in (d["Notes"],) if c]

        ov = OVERRIDES.get(name)
        if ov:
            plat = ov.get("platform", plat)
            url = ov.get("url", url)
            online = ov.get("online", online)
            conf = ov.get("conf", conf)
            clauses.append(ov["why"])
        if name in HELD:
            online = ""
            plat = LABEL2SLUG[d["Booking Platform"]]
            url = d["Booking URL (scrape target)"]
            clauses.append(HELD_WHY)

        if d.get("Phone"):
            clauses.append(f"Pro shop {d['Phone']}")
        if d.get("Region"):
            clauses.append(f"{d['Region']} region")
        clauses.append(f"From {REV} ({d['Verification']})")

        out.append({
            "Course Name": name,
            "Display Name": o.get("Display Name", ""),
            "City": d["City"],
            "Zip": o.get("Zip", ""),
            "Type": d["Type"],
            "Website": d["Website"],
            "Booking Platform": plat,
            "Booking URL": url,
            "Online Booking": online,
            "Confidence": conf,
            "Notes": " | ".join(clauses),
        })
        notes_of[name] = out[-1]

    for name, why in CLOSED.items():
        o = old.get(name, {})
        out.append({
            "Course Name": name,
            "Display Name": o.get("Display Name", ""),
            "City": o.get("City", ""),
            "Zip": o.get("Zip", ""),
            "Type": "Closed",
            "Website": "",
            "Booking Platform": "",
            "Booking URL": "",
            "Online Booking": "no",
            "Confidence": "high",
            "Notes": (f"Dropped from {REV}; researched 2026-07-30 and {why}. "
                      "Kept as Type: Closed so the finding survives - Closed "
                      "rows are excluded from both registry.json and the "
                      "directory, so this cannot render as a bookable venue. | "
                      + (o.get("Notes", "").split(" | From ")[0] or "")),
        })

    out.extend(EXTRA_ROWS)

    # ---- guard: no two shipping rows may share a booking URL -----------------
    seen: dict[str, list[str]] = {}
    for r in out:
        if r["Online Booking"] == "yes" and r["Booking Platform"] and r["Booking URL"]:
            seen.setdefault(r["Booking URL"], []).append(r["Course Name"])
    shared = {u: n for u, n in seen.items() if len(n) > 1}
    for u, names in shared.items():
        print(f"SHARED URL ({len(names)}): {u}\n    {names}")
    if shared:
        print("^ each of these must pin a per-course id in build_registry.EXTRA_IDS")

    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(out)

    import collections
    print(f"\nwrote {len(out)} rows")
    print("Online Booking:", dict(collections.Counter(r["Online Booking"] for r in out)))
    print("platforms:", dict(collections.Counter(
        r["Booking Platform"] for r in out if r["Online Booking"] == "yes")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
