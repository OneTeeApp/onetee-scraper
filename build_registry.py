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
    "golfnow": re.compile(r"golfnow\.com/tee-times/facility/(\d+)-([a-z0-9-]+)"),
    "teesnap": re.compile(r"https?://([a-z0-9-]+)\.teesnap\.net"),
    "quick18": re.compile(r"https?://([a-z0-9-]+)\.quick18\.com"),
    "noteefy": re.compile(r"booking\.noteefy\.app/e/([0-9a-f-]+)"),
    "foretees": re.compile(r"foretees\.com/.*clubKey=([A-Za-z0-9]+)&cid=(\d+)"),
}

# extra IDs known from research that aren't visible in the URL
EXTRA_IDS = {
    # (Buffalo Run's old hardcoded facility_id 12190 was stale -> HTTP 500;
    #  the adapter now discovers facility ids at runtime, so it's removed.)
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

    # TeeItUp: the book.teeitup.com vanity subdomain is NOT always the kenna
    # x-be-alias. Captured the real alias each course's booking page sends to
    # phx-api-be-east-1b.kenna.io/alias/<alias>/facilities and override it here.
    # (Rollingstone had no booking URL in the CSV, so this also supplies it.)
    "omni interlocken resort golf club": {"alias": "interlocken-golf-club-ohr"},
    "pole creek golf club":              {"alias": "pole-creek-golf-club"},
    "raindance national resort & golf":  {"alias": "raindance-national-resort-golf"},
    "rollingstone ranch golf club":      {"alias": "rollingstone-ranch"},

    # Club Prophet (cps.golf): the adapter discovers courseIds + websiteId at
    # runtime via OnlineCourses from just the tenant subdomain. Indian Peaks is
    # pinned (captured live) as a guaranteed anchor in case discovery ever fails
    # for a tenant.
    "indian peaks golf course": {"website_id": "f04abbc1-368f-40f4-096d-08d89aea9574",
                                 "course_ids": [10, 11]},
    # Pinned via discover3 browser probe (GetAllOptions), July 2026:
    "legacy ridge golf course":  {"website_id": "be7f2728-0758-4a72-fe80-08d97849167d",
                                  "course_ids": [1, 4]},   # 4 = LR Back 9
    "walnut creek golf preserve": {"website_id": "be7f2728-0758-4a72-fe80-08d97849167d",
                                   "course_ids": [2]},
    "mariana butte golf course": {"website_id": "e0496558-918b-4f2d-44dc-08dbf84ad30b",
                                  "course_ids": [3]},
    "gypsum creek golf course":  {"website_id": "36a7e810-d311-43dc-8326-08db37856ea4",
                                  "course_ids": [1, 2]},   # 2 = offseason sheet
    # ForeUp munis: Patty Jewett 401s without a booking_class; pin the classes
    # that returned 200 in discover3.
    "patty jewett golf course":  {"booking_class": "1339"},
    "valley hi golf course":     {"booking_class": "4502"},
    # Pin every cps.golf tenant's websiteId + courseIds (captured via the
    # tenant's own GetAllOptions). Runtime discovery works from a residential IP
    # but returns empty/garbled from GitHub's datacenter IP, so pinning lets the
    # adapter skip discovery and run only token->register->teetimes (which does
    # work headless). Eagle Trace / Emerald Greens / University of Denver 404 on
    # the token endpoint even residentially -> inactive CPS setup, left out.
    "cattail creek golf course":   {"website_id": "d6b99326-b2db-4033-44db-08dbf84ad30b", "course_ids": [1]},
    "flatirons golf course":       {"website_id": "d0c1d3f9-28c7-4f79-8ee1-08d926a72623", "course_ids": [1]},
    "fossil trace golf club":      {"website_id": "b6c22f3a-944a-46e9-020e-08da90168fb2", "course_ids": [1, 2, 3]},
    "green valley ranch golf club":{"website_id": "e6b92812-d6c4-4f86-7eea-08d9fadf154d", "course_ids": [1, 2, 3, 4]},
    "haymaker golf course":        {"website_id": "b74c91b6-8f7d-4db2-3fd0-08d9f56b5de1", "course_ids": [1, 2, 4]},
    "indian tree golf course":     {"website_id": "e6d9cd59-8d46-4334-8601-08dad3012d25", "course_ids": [1]},
    "mariana butte golf course":   {"website_id": "e0496558-918b-4f2d-44dc-08dbf84ad30b", "course_ids": [3]},
    "red hawk ridge golf course":  {"website_id": "1ca33515-0bb5-4f13-3ebb-08d9d9c521b3", "course_ids": [1, 2]},
    "the olde course at loveland": {"website_id": "e1be30d2-b87c-40ec-44dd-08dbf84ad30b", "course_ids": [2]},
}

# adapters that can actually fetch today
IMPLEMENTED = {"foreup", "teeitup", "chronogolf", "clubprophet", "clubcaddie",
               "membersports", "quick18", "teesnap", "foretees"}


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
        return {"golfnow_facility_id": g[0], "golfnow_slug": g[1].removesuffix("/search")}
    if platform in ("teesnap", "quick18"):
        return {"subdomain": g[0]}
    if platform == "noteefy":
        return {"venue_guid": g[0]}
    if platform == "foretees":
        return {"club_key": g[0], "cid": g[1]}
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
