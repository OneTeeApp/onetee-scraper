"""One-shot: convert the Florida research xlsx into florida_golf_courses_booking.csv.

Input: Florida_Golf_Courses_Booking_Directory.xlsx (Courses sheet, 658 rows) —
columns: Course Name, City, Region, Type, Booking Platform, Booking URL,
Verification, Notes, Phone, Website.

Output matches the repo's state-CSV contract (see virginia_*.csv):
Course Name, Display Name, City, Zip, Type, Website, Booking Platform,
Booking URL, Online Booking, Confidence, Notes — with Region and Phone packed
into Notes the way the VA sheet does, since build_directory.find_phone() reads
the number back out of Notes.

Platform mapping notes (the decisions that aren't mechanical):
* Troon -> golfwithaccess. troon.com/course/<t>/reserve-tee-time IS the Golf
  With Access widget (same tenant slug); the course uuid still needs a probe.
* ClubHouse Online -> clubessential (ClubHouse Online E3 is Clubessential's
  product; host + GOLFCOURSE ids need the one-time widget capture).
* Total e Integrated -> totale (tenant/label pins to come from a probe).
* Trump Doral's booktrump.com portal and course-site embedded widgets have no
  adapter: other:booktrump / other:native.
* Golfback (15 courses, uuid right in the URL) has no adapter yet ->
  other:golfback; native_probe.py already recognizes the host shape.
"""
import csv
import re

import openpyxl

SRC = "/root/.claude/uploads/41a5397a-6dff-566c-82d4-8d01bbee58b5/ebf78af5-Florida_Golf_Courses_Booking_Directory.xlsx"
OUT = "florida_golf_courses_booking.csv"

PLATFORM_MAP = {
    "TeeItUp (Golf Genius)": "teeitup",
    "EZLinks (TeeOff)": "ezlinks",
    "ForeUp": "foreup",
    "Chronogolf (Lightspeed)": "chronogolf",
    "Teesnap": "teesnap",
    "Club Prophet (CPS)": "clubprophet",
    "Club Caddie": "clubcaddie",
    "Agilysys rGuest": "rguest",
    "GolfNow": "golfnow",
    "Quick18": "quick18",
    "TeeQuest": "teequest",
    "Golf With Access": "golfwithaccess",
    "Troon": "golfwithaccess",
    "Total e Integrated": "totale",
    "ClubHouse Online": "clubessential",
    "Golfback": "other:golfback",
    "WebTrac": "other:webtrac",
    "Eagle Club Systems": "other:eagleclubsystems",
    "CourseRev.AI": "other:courserev",
    "Acuity Scheduling": "other:acuity",
    "Chelsea Reservations": "other:chelsea",
    "LinkLine Online (ClubLink)": "other:linkline",
    "Buz Club Software": "other:buz",
    "Golf18Network": "other:golf18network",
    "Bailey Reservations": "other:bailey",
    "TeeWire": "other:teewire",
    "Course website (embedded widget)": "other:native",
}

# Courses the notes prove are gone (not renovation-with-a-reopening-date).
CLOSED = {"Sherwood Golf Club", "Fort Myers Beach Golf Club"}

# On-base courses: a DoD ID or visitor pass gates every booking method, so the
# directory's military label (not "Call to book") is the honest one. A.C. Read
# is NAS Pensacola's course (Navy MWR page); Eglin's is on Eglin AFB.
MILITARY = {"A.C. Read Golf Club", "Eglin Golf Course"}

EXTRA_NOTE = {
    "golfwithaccess-troon": "Troon-managed; the troon.com reserve page fronts "
                            "the Golf With Access widget (course uuid needs probe)",
    "clubessential": "ClubHouse Online E3 (Clubessential) — host/course ids "
                     "need one-time widget capture",
}


def main() -> None:
    wb = openpyxl.load_workbook(SRC, read_only=True)
    rows = [r for r in list(wb["Courses"].iter_rows(values_only=True))[1:] if r[0]]
    out = []
    for name, city, region, typ, plat, url, ver, notes, phone, website in rows:
        name, notes, url = (name or "").strip(), (notes or "").strip(), (url or "").strip()
        plat, ver = (plat or "").strip(), (ver or "").strip()
        extra = []

        if plat == "Unknown":
            if "booktrump.com" in url:
                platform, online, conf = "other:booktrump", "yes", "high"
            elif ver == "VERIFIED" and url:
                platform, online, conf = "other:native", "yes", "high"
            else:
                platform, online, conf = "", "unknown", ""
        elif plat == "None (phone only)":
            platform, online = "", "no"
            conf = "low" if ver == "UNVERIFIED" else ""
        else:
            platform = PLATFORM_MAP[plat]
            online = "yes"
            conf = "high" if ver in ("VERIFIED", "UPDATED") else "medium"
            if plat == "Troon":
                extra.append(EXTRA_NOTE["golfwithaccess-troon"])
            elif platform == "clubessential":
                extra.append(EXTRA_NOTE["clubessential"])

        if name in CLOSED:
            typ = "Closed"
        elif name in MILITARY:
            typ = "Military"

        note_bits = [notes] if notes else []
        note_bits += extra
        if region:
            note_bits.append(f"Region: {region}")
        if phone:
            note_bits.append(f"Phone: {phone}")
        out.append({
            "Course Name": name,
            "Display Name": "",
            "City": (city or "").strip(),
            "Zip": "",
            "Type": typ or "",
            "Website": (website or "").strip(),
            "Booking Platform": platform,
            "Booking URL": url if online == "yes" else url,
            "Online Booking": online,
            "Confidence": conf,
            "Notes": " | ".join(note_bits),
        })

    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    print(f"wrote {OUT}: {len(out)} rows")
    print("online:", dict(Counter(r["Online Booking"] for r in out)))
    print("platforms:", dict(Counter(r["Booking Platform"] for r in out if r["Booking Platform"])))


if __name__ == "__main__":
    main()
