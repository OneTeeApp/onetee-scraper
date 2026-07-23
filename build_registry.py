"""Generate registry.json from colorado_golf_courses_booking.csv.

Extracts platform-specific IDs out of each booking URL so the adapters can
query APIs directly. Run this whenever the CSV changes:

    python build_registry.py
"""
from __future__ import annotations

import csv
import json
import re

SRC = "colorado_golf_courses_booking.csv"
OUT = "registry.json"

PATTERNS = {
    "foreup": re.compile(r"foreupsoftware\.com/index\.php/booking(?:/index)?/(\d+)(?:/(\d+))?"),
    "teeitup": re.compile(r"https?://([a-z0-9-]+)\.(?:book(?:-v2)?\.teeitup\.(?:com|golf)|play\.teeitup\.com)"),
    "clubprophet": re.compile(r"https?://([a-z0-9]+)\.cps\.golf"),
    "chronogolf": re.compile(r"chronogolf\.(?:com|ca)/club/([a-z0-9-]+)"),
    "clubcaddie": re.compile(r"apimanager-(cc\d+)\.clubcaddie\.com/webapi/view/([a-z]+)"),
    "membersports": re.compile(r"app\.membersports\.com/(?:tee-times|book-linked-clubs-tee-time|custom)/(\d+)/(\d+)"),
    "ezlinks": re.compile(r"https?://([a-z0-9-]+)\.ezlinksgolf\.com"),
    "golfnow": re.compile(r"golfnow\.com/tee-times/facility/(\d+)-"),
    "teesnap": re.compile(r"https?://([a-z0-9-]+)\.teesnap\.net"),
    "quick18": re.compile(r"https?://([a-z0-9-]+)\.quick18\.com"),
}

# extra IDs known from research that aren't visible in the URL
EXTRA_IDS = {
    "buffalo run golf course": {"facility_id": "12190"},
    # Denver MemberSports courses are separate clubs linked in one "Denver
    # Courses" group; the booking URL only carries the group (3660/4711), so
    # override each with its real golfClubId/golfCourseId (from the group's
    # member list). Without this they'd all query City Park and collapse to one.
    "evergreen golf course":     {"club_id": "3691", "secondary_id": "4751"},
    "wellshire golf course":     {"club_id": "3831", "secondary_id": "4928"},
    "overland park golf course": {"club_id": "3755", "secondary_id": "4827"},
    "harvard gulch golf course": {"club_id": "3713", "secondary_id": "4781"},
    "willis case golf course":   {"club_id": "3833", "secondary_id": "4932"},
    "kennedy golf course":       {"club_id": "3629", "secondary_id": "20573"},
    # city-park stays 3660/4711 (correct as extracted)
}

# adapters that can actually fetch today
IMPLEMENTED = {"foreup", "teeitup", "chronogolf", "clubprophet", "clubcaddie",
               "membersports", "quick18", "teesnap"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_ids(platform: str, url: str) -> dict:
    m = PATTERNS.get(platform, re.compile(r"$^")).search(url or "")
    if not m:
        return {}
    g = m.groups()
    if platform == "foreup":
        return {"course_id": g[0], "schedule_id": g[1]}
    if platform == "teeitup":
        return {"alias": g[0]}
    if platform == "clubprophet":
        return {"tenant": g[0]}
    if platform == "chronogolf":
        key = "club_id" if g[0].isdigit() else "slug"
        return {key: g[0], "club_uuid": None}
    if platform == "clubcaddie":
        return {"shard": g[0], "view_token": g[1]}
    if platform == "membersports":
        return {"club_id": g[0], "secondary_id": g[1]}
    if platform == "ezlinks":
        return {"portal": g[0]}
    if platform == "golfnow":
        return {"golfnow_facility_id": g[0]}
    if platform in ("teesnap", "quick18"):
        return {"subdomain": g[0]}
    return {}


def main() -> None:
    courses = []
    with open(SRC) as f:
        for row in csv.DictReader(f):
            if row["Online Booking"] != "yes" or not row["Booking Platform"]:
                continue
            platform = row["Booking Platform"]
            ids = extract_ids(platform, row["Booking URL"])
            ids.update(EXTRA_IDS.get(row["Course Name"].lower(), {}))
            if platform.startswith("other:"):
                status = "unsupported"
            elif platform not in IMPLEMENTED:
                status = "experimental"          # golfnow / ezlinks
            elif platform == "foreup" and not ids.get("schedule_id"):
                status = "needs_ids"
            elif platform == "chronogolf" and not ids.get("club_uuid"):
                status = "needs_ids"             # uuid harvested at runtime
            else:
                status = "ready"
            courses.append({
                "slug": slugify(row["Course Name"]),
                "name": row["Course Name"],
                "city": row["City"],
                "platform": platform,
                "booking_url": row["Booking URL"],
                "ids": ids,
                "status": status,
                "confidence": row["Confidence"],
                "notes": row["Notes"],
            })
    with open(OUT, "w") as f:
        json.dump({"generated_from": SRC, "courses": courses}, f, indent=1)
    from collections import Counter
    print(f"wrote {OUT}: {len(courses)} bookable courses")
    print(dict(Counter(c['status'] for c in courses)))


if __name__ == "__main__":
    main()
