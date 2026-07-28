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
    "membersports": re.compile(r"app\.membersports\.com/(?:tee-times|book-linked-clubs-tee-time|custom)/(\d+)/(\d+)(?:/(\d+))?(?:/(\d+))?"),
    "ezlinks": re.compile(r"https?://([a-z0-9-]+)\.ezlinks(?:golf)?\.com"),
    "golfnow": re.compile(r"golfnow\.com/tee-times/facility/(\d+)-([a-z0-9-]+)"),
    "teesnap": re.compile(r"https?://([a-z0-9-]+)\.teesnap\.net"),
    "quick18": re.compile(r"https?://([a-z0-9-]+)\.(?:quick18|play18)\.com"),
    "noteefy": re.compile(r"booking\.noteefy\.app/e/([0-9a-f-]+)"),
    "foretees": re.compile(r"foretees\.com/.*clubKey=([A-Za-z0-9]+)&cid=(\d+)"),
    "supersaas": re.compile(r"supersaas\.com/schedule/([^/]+)/([^/?#]+)"),
    "rguest": re.compile(r"book\.rguest\.com/onecart/golf/courses/(\d+)/([a-z0-9-]+)"),
    "courseco": re.compile(r"https?://([a-z0-9-]+)\.totaleintegrated\.net"),
    # TeeQuest ships two skins. Legacy is teetimes.teequest.com/<site>; v2 is
    # bookateetime.teequest.com/course/<site>. Same operator, different
    # request shape, so the host is captured alongside the id.
    "teequest": re.compile(
        r"https?://(teetimes|bookateetime)\.teequest\.com/(?:course/)?(\d+)"),
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
    # Kennedy's three sheets are NOT all reachable from one configurationTypeId
    # (see probe-results/diag2.txt): cfg 0 is the Par 3 sheet, cfg 1 is Babe
    # Lind / Creek and cfg 2 is West 9 only. Pinned to cfg 0, the two 18-hole
    # configurations were invisible and course_label was always blank.
    "kennedy golf course":       {"club_id": "3629", "secondary_id": "20573",
                                  "config_ids": [0, 1, 2]},
    # city-park stays 3660/4711 (correct as extracted)

    # Total-e-Integrated: one DNN tee sheet per tenant lists every course
    # interleaved, each row labelled with its course. `tenant` is the
    # *.totaleintegrated.com subdomain; `label` is the exact course name the
    # sheet prints, which browser_totale matches rows on. The Sun City West
    # seven share one tenant; Ken McDonald is its own.
    # Sun City West moved to Total-e's replacement platform 2026-07-28, so these
    # seven are courseco rows now, not totale. Three things have to be pinned by
    # hand and none of them is in the booking URL:
    #   * tenant  — the booking SITE subdomain, used for the Origin header.
    #   * gateway — the API subdomain, read off the page's own
    #     window.__config.apiBaseUrl. NOT derivable from tenant: Ken McDonald's
    #     site is kenmcdonald but its gateway is courseco. Guessing 400s.
    #   * course_id — the course code, a human string with spaces.
    # The booking URL deliberately still points at the legacy .com PUBLIC page,
    # because the .net portal bounces anonymous visitors to a login, so the
    # courseco URL regex never fires for these rows and everything comes from
    # here.
    "deer valley golf course":   {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "DEER VALLEY"},
    "desert trails golf course": {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "DESERT TRAILS"},
    "echo mesa golf course":     {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "ECHO MESA"},
    "grandview golf course":     {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "GRANDVIEW"},
    "pebblebrook golf course":   {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "PEBBLEBROOK"},
    "stardust golf course":      {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "STARDUST"},
    "trail ridge golf course":   {"tenant": "suncitywest", "gateway": "suncitywest", "course_id": "TRAIL RIDGE"},
    # Ken McDonald migrated off the legacy platform 2026-07-28
    # (playkenmcdonald.totaleintegrated.com answers Cloudflare 525) and is a
    # courseco row now. Its tenant comes free from the new booking URL, but its
    # GATEWAY does not and cannot be guessed from it: the site is
    # kenmcdonald.totaleintegrated.net while the API is
    # courseco-gateway.totaleintegrated.net, because CourseCo is the management
    # company. Read from the page's own window.__config.apiBaseUrl.
    "ken mcdonald golf course":  {"gateway": "courseco"},

    # rGuest: Wildfire's two courses are two registry venues sharing ONE
    # property (tenant 2418), so each claims its own sheet by course_id.
    # Verified live 2026-07-27: Faldo 519, Palmer 520. We-Ko-Pa (2093/wekopa)
    # and Camelback (2281/camelback-golf-club) are single venues covering both
    # of their courses, so they pin nothing and get labelled sub-courses.
    "wildfire golf club - faldo course":  {"course_id": 519},
    "wildfire golf club - palmer course": {"course_id": 520},

    # Virginia additions (2026-07-28), all read off each platform's own API in
    # a browser probe with serving controls — never guessed.
    # The sixth vanity-host-is-not-the-alias instance: the booking host
    # meadowcreek-golf-course-va 404s at kenna; the page's own /alias/ call
    # names meadow-creek-golf-course-v2 (facility 18684).
    "meadowcreek golf course": {"alias": "meadow-creek-golf-course-v2"},
    # Quantico's chronogolf club lists a "Simulator Bay" course with
    # online_booking_enabled=true beside the golf course; unpinned, the adapter
    # would publish simulator slots as tee times. Pin the golf course only.
    "medal of honor golf course": {"course_ids": [16571]},
    # Two venues sharing one chronogolf club (19025, somerset-meadows-farms).
    # Unpinned, each would fetch the whole club and publish the other's sheet —
    # the Biltmore shape. Course ids from the club's own /courses list:
    # Meadows Farms sells three 18-hole routing combos, Somerset Farms one.
    "meadows farms golf course":  {"course_ids": [23369, 23371, 23370]},
    "somerset farms golf course": {"course_ids": [23368]},
    # Ford's Colony: three TeeItUp tenants, one per course. Blue Heron and
    # Marsh Hawk are vanity-host-is-not-the-alias instances seven and eight —
    # each alias read off its own booking page's /alias/ network call.
    "ford's colony country club - blue heron course": {"alias": "fords-colony"},
    "ford's colony country club - marsh hawk course":
        {"alias": "ford-s-colony-country-club-marsh-hawk"},
    # Two VA Club Prophet tenants, ids captured 2026-07-28 via the anonymous
    # token -> GetAllOptions flow on each tenant's own origin.
    "old trail golf club": {"website_id": "5be83a0d-e758-435b-138f-08dae2b1ae8f",
                            "course_ids": [1]},
    "williamsburg national golf club":
        {"website_id": "ffa2bdfb-f752-4d47-b541-08d87f6a4174", "course_ids": [1, 2]},

    # Two AZ TeeItUp courses whose kenna alias is nothing like their booking
    # host — found 2026-07-27 by reading each booking page's OWN /alias/ call
    # instead of guessing spellings (three guesses each had 404'd). Junior
    # National still answers to its former name. Both verified serving.
    "omni tucson national":      {"alias": "tucson-national-omni"},
    "junior national golf club": {"alias": "tres-rios-gc-at-estrella-mountain-park"},

    # Golf With Access course uuids for five courses that turned out to book
    # here rather than where the directory said. Captured 2026-07-27 from each
    # page's own /api/v1/tee-times courseIds param (the SSR courses:[] array
    # the older entries came from is gone - the site is client-rendered now),
    # and every one verified serving before being pinned. Antelope Hills and
    # Kierland each expose only ONE of their multiple nines this way; the
    # others are a later addition, not a blocker.
    "san pedro golf course":       {"course_id": "d10ed1e5-1bf2-4e52-b312-3c8ee622b64c", "tenant": "san-pedro-golf-course"},
    "antelope hills golf course":  {"course_id": "e2aeb67d-6e7f-41c5-90ef-df6dca5f5c72", "tenant": "antelope-hills-golf-course"},
    "arizona national golf club":  {"course_id": "61cc7d8d-3c22-44f6-ad4d-1799a515249a", "tenant": "arizona-national-golf-club"},
    "golf club at eagle mountain": {"course_id": "698b68b9-908e-416e-8368-a043e2a90072", "tenant": "eagle-mountain-golf-club"},
    "westin kierland golf club":   {"course_id": "ab5ad653-b217-4119-bcb7-80dd0aecffaa", "tenant": "the-westin-kierland-golf-club"},

    # Fountain of the Sun moved off TeeItUp to Club Prophet. Its tenant uses the
    # ZERO websiteId (captured from the app's own sessionStorage) rather than a
    # real GUID like the Colorado tenants, and exposes a single course id. The
    # full anonymous token -> register -> TeeTimes flow was replayed against it
    # before pinning: 8 tee times at +2, HTTP 200.
    "fountain of the sun country club": {"website_id": "00000000-0000-0000-0000-000000000000", "course_ids": [1]},

    # EZLinks portals VERIFIED 2026-07-28 to front exactly one venue: every
    # course id the portal exposes belongs to this club, so its slots are ours
    # without name matching. This is what rescued them — their sheets name the
    # course differently from our directory ("Mt." vs "Mount", "PV -
    # South/West", "The O'odham Course", "- Devil's Claw"), so name matching
    # threw away portals that were answering 200 with real rows.
    #
    # Only ever set this after reading the portal's own init.Courses list and
    # confirming every id is this club's. The Legend at Arrowhead is the
    # cautionary case: its pinned portal arcisgolfazpp lists 19 courses and
    # none of them is Legend, so it gets no flag and falls back to name
    # matching, which correctly yields nothing until its real portal is found.
    "mount graham golf course":     {"sole_portal": True},
    "palm valley golf club":        {"sole_portal": True},
    "talking stick golf club":      {"sole_portal": True},
    "whirlwind golf club at wild horse pass": {"sole_portal": True},
    "bear creek golf complex":      {"sole_portal": True},
    "the foothills golf club":      {"sole_portal": True},
    "lookout mountain golf club":   {"sole_portal": True},

    # Three CO courses were tagged with a NEIGHBOUR's golfClubId because the
    # city portal's booking URL is shared between two courses. The club-id scan
    # (probe-results/msscan.txt) asked each club what it actually owns and
    # settled them: 3724 is Highland Hills, not Boomerang; 3739 is Lincoln Park,
    # while 3821 is Tiara Rado. Left uncorrected, each of these served its
    # neighbour's tee sheet under its own name.
    "boomerang golf links":      {"club_id": "3642", "secondary_id": "4686"},
    "lincoln park golf course":  {"club_id": "3739", "secondary_id": "4808"},
    # highland-hills stays 3724/4793 and tiara-rado stays 3821/4918 — both
    # confirmed correct by the same scan.
    #
    # Meadows was blanked here for a while: club 3697 owns only the three
    # sheets named "Foothills", the CSV row had been copied from Foothills'
    # URL (same parks district, same website), and scraping it would have
    # republished Foothills' tee times under the Meadows name. RESOLVED
    # 2026-07 — foothillsgolf.org also links online-store/3811/4906/225, and a
    # cfg 0-8 sweep of club 3811 returns items named "Meadows" and "Meadows
    # Back Nine" with ZERO (courseId, name) overlap against 3697
    # (probe-results/verify_membersports.txt). The CSV now carries the real
    # tee-times URL, so no override is needed and the blank entry is gone.

    # TeeItUp: the book.teeitup.com vanity subdomain is NOT always the kenna
    # x-be-alias. Captured the real alias each course's booking page sends to
    # phx-api-be-east-1b.kenna.io/alias/<alias>/facilities and override it here.
    "omni interlocken resort golf club": {"alias": "interlocken-golf-club-ohr"},
    "pole creek golf club":              {"alias": "pole-creek-golf-club"},
    "raindance national resort & golf":  {"alias": "raindance-national-resort-golf"},
    # Rollingstone's entry ({"alias": "rollingstone-ranch"}) is gone on
    # purpose: the alias was confirmed real and confirmed EMPTY on every date
    # (registry note, 2026-07-26) — Troon does not publish that sheet
    # anonymously. The course actually sells through Golf With Access, and the
    # CSV now says so. Re-adding the alias here would put a dead teeitup ids
    # blob on an other:golfwithaccess row.
    # Golden Hills: the host is golden-hills-golf-club-az.book.teeitup.com but
    # kenna has never heard of that alias — 404 "Booking Engine Settings not
    # found" on every route, while every sibling Arizona alias answers
    # (probe-results/diag_golden_hills.txt). Dropping the -az suffix resolves,
    # lists exactly facility 1295 "Golden Hills Golf Club" (so the pinned id
    # was right all along) and returns 51 slots for tomorrow. The club's former
    # name, arizona-golf-resort, resolves to the identical sheet. The
    # booking_url stays on the -az host: it serves a real 29KB tenant page,
    # while arizona-golf-resort.book.teeitup.com serves the same ~10KB shell as
    # a deliberately nonsense subdomain (diag_kenna_routes.txt section 1) — the
    # vanity host and the API alias are two different namespaces.
    "golden hills golf club":            {"alias": "golden-hills-golf-club"},

    # Three more AZ TeeItUp courses whose pinned alias 404s at kenna while the
    # course is very much selling — the same class of bug as Golden Hills above.
    # Found 2026-07-27 by a browser probe fired from a booking origin, with BOTH
    # controls serving (coldwater 226, rancho manana 249), so the empties in that
    # run are real verdicts rather than the 429 poisoning that invalidated the two
    # earlier probe attempts. Each corrected alias resolves to exactly one facility
    # and returns live inventory: Ahwatukee CC -> 1294 (491 slots / 6 dates),
    # Sierra Vista -> 9585 (382), Ventana Canyon -> 464 (247, thinning past ~2wk).
    # All three sat at "ready" reading zero, indistinguishable from a dark sheet.
    "ahwatukee country club":                     {"alias": "ahwatukee-country-club"},
    "sierra vista golf center at pueblo del sol": {"alias": "pueblo-del-sol-country-club"},
    "ventana canyon golf & racquet club":         {"alias": "ventana-canyon"},

    # Pinetop Lakes, same bug class again: the registry pinned the VANITY HOST
    # (pinetop-lakes-public, from the booking URL) as if it were the kenna
    # alias. It is not — the booking page's own /alias/ call names
    # pinetop-lakes-golf-and-country-club, which resolves to exactly one
    # facility (14204 "Pinetop Lakes Golf & Country Club") and returned 27
    # slots for tomorrow when probed from a booking origin 2026-07-28.
    # Later dates read zero, and that is real: this is a 6,000ft mountain
    # course with a short booking window, not a dark sheet. Read the alias off
    # the page; never infer it from the host.
    "pinetop lakes golf & country club": {"alias": "pinetop-lakes-golf-and-country-club"},

    # Broadlands: the public booking front door is Noteefy (that's what the
    # Booking URL must point users to), but the tee sheet is scraped from the
    # Chronogolf marketplace API that mirrors it — so pin the chronogolf slug
    # here since it's no longer extractable from the (Noteefy) booking URL.
    "broadlands golf course": {"slug": "broadlands-golf-course"},

    # Golf With Access (Troon): the bookable course uuid is NOT in the booking
    # URL — it lives in each tenant page's SSR courses:[{id,name}] array, so it
    # is pinned here, captured live July 2026 (browser network capture). Every
    # id below was verified to return that course's own sheet, and the adapter
    # re-asserts course.id on each slot. The facility-named id is always a dead
    # aggregate (returns 0), so shared tenants pin the per-course id: the five
    # Tucson munis share the tucson-city-golf tenant, El Conquistador + Pusch
    # Ridge share el-conquistador-golf-club, and El Con/Starr Pass book through
    # a sub-course id ("Conquistador Course" / "Gambler/Pioneer") rather than
    # the headline id.
    "rollingstone ranch golf club": {"course_id": "4ed9004a-c17f-4e52-aa86-d6c0bf46e869", "tenant": "rollingstone-ranch-golf-club"},
    "lake monticello golf course": {"course_id": "324901f5-cbf7-4575-8304-f3b062efe61b", "tenant": "lake-monticello-golf-course"},
    "potomac shores golf club":    {"course_id": "10c68f5d-6ad2-4e04-ba84-9d96b79688c5", "tenant": "potomac-shores-golf-club"},
    "poston butte golf club":       {"course_id": "1107b804-2789-450d-bd61-8611fa9f742c", "tenant": "poston-butte-golf-club"},

    # ClubEssential / NetCaddy. A ClubEssential club site is a CMS whose
    # booking page URL names a module, not a course, so nothing is derivable
    # from it. These three values are exactly what the club's own public
    # widget sends: host, the GOLFCOURSE ids, and the SiteID every row of the
    # response carries. The adapter drops any row whose SiteID or CourseId is
    # not one of these — mcconnellgolf.com fronts a dozen-plus clubs and a
    # host-global id space is how another club's tee sheet ends up published
    # under our name. Captured 2026-07-28; 86 is offered by the widget but
    # returns nothing, 84 is the live 18.
    "pete dye river course of virginia tech": {
        "host": "www.mcconnellgolf.com", "course_ids": [86, 84],
        "site_id": 2060},

    "las colinas golf club":        {"course_id": "2c9b2f0d-72b7-49a3-a428-ee7efef5ebbf", "tenant": "las-colinas-golf-club"},
    "el conquistador golf club":    {"course_id": "4632ffdb-fe79-46ec-ab9b-b3b70bb8a965", "tenant": "el-conquistador-golf-club"},
    "pusch ridge golf course":      {"course_id": "361624de-ac95-4208-bdf7-4af6e84f27e1", "tenant": "el-conquistador-golf-club"},
    "dell urich golf course":       {"course_id": "e37da0d2-865e-4ea0-9cc8-3331440ad82f", "tenant": "tucson-city-golf"},
    "el rio golf course":           {"course_id": "7bb875b1-777d-43bb-8802-6c15c540dfa7", "tenant": "tucson-city-golf"},
    "fred enke golf course":        {"course_id": "c70e6b85-4076-4cb8-9cb3-419c97586162", "tenant": "tucson-city-golf"},
    "randolph north golf course":   {"course_id": "78c4ad12-e482-43d5-a39e-1d6115c8b09b", "tenant": "tucson-city-golf"},
    "silverbell golf course":       {"course_id": "e3525d5b-9dea-4c91-a704-0d243c694ac6", "tenant": "tucson-city-golf"},
    "the club at starr pass":       {"course_id": "eb039b63-0c58-4033-b8e2-cca31ca850d3", "tenant": "the-club-at-starr-pass"},

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
    # The Arizona Biltmore's two rows share one Chronogolf club (18077), whose
    # course list is {21028: "Estates", 21027: "Links"} — the same two names our
    # rows carry, so the pairing is read off the platform rather than guessed.
    # Without pinning, each row would fetch BOTH courses and every tee time
    # would be published twice, once under each course's slug.
    "arizona biltmore golf club - estates course": {"course_ids": [21028]},
    "arizona biltmore golf club - links course":   {"course_ids": [21027]},
    # Arizona cps.golf tenants (GetAllOptions, July 2026):
    "del lago golf club":  {"website_id": "03dffe68-f3cf-4563-c22c-08ddcacdb8cb", "course_ids": [1]},
    "sewailo golf club":   {"website_id": "a719a286-7fcc-4c03-d59b-08db1f359d2a", "course_ids": [1]},
    "the views golf club": {"website_id": "8c0a1716-ea2b-4c84-adff-08d8df3c1472", "course_ids": [1, 2, 3]},
}

# Courses the platform itself, when asked, said it could not serve — recorded
# by slug so they survive the status chain being fixed underneath them.
#
# The chronogolf and foreup gates below used to reject every row for an
# identifier the adapters resolve on their own, so 45 courses sat in needs_ids
# by accident. Fixing the gates flips them to "ready", which is right for 35 of
# them and wrong for these: they would be attempted on every scrape, fail every
# time, and the registry would call them ready while doing it. A course that
# cannot be fetched must say so, and say WHY, or the next person re-derives the
# same answer.
#
# Evidence: probe-results/needs-ids.json, 2026-07-26. Each entry is
# (status, reason) — `unsupported` means no identifier would help, `needs_ids`
# means a human still can.
PROBED_HOLDS: dict[str, tuple[str, str]] = {
    # ForeUp booking pages that load but carry no schedule_id at all, so
    # discover_ids() comes back empty and fetch() has nothing to query.
    "meeker-golf-course": ("needs_ids", "foreup booking page 22597 exposes no "
                           "schedule_id"),
    "snowflake-municipal-golf-course": ("needs_ids", "foreup booking page 1858 "
                                        "exposes no schedule_id"),
    # The Wigwam's three courses share one Chronogolf club (2454) whose courses
    # are named Gold / Blue / Red, while our rows are Gold / Heritage /
    # Patriot. Gold is unambiguous; which of Heritage and Patriot is Blue and
    # which is Red is not, and the resort's own site now uses Gold/Blue/Red
    # without mapping the old names. Left un-pinned, all three rows would fetch
    # all three courses and publish each course's tee sheet three times under
    # three names. Guessing the pairing would publish one course's times under
    # the other's name — the same failure the Highland Hills / Boomerang note
    # above exists to prevent. So: held until somebody confirms the pairing.
    "wigwam-golf-club-gold-course": ("needs_ids", "shares chronogolf club 2454 "
                                     "with two sibling rows; course pinning "
                                     "unresolved"),
    "wigwam-golf-club-heritage-course": ("needs_ids", "chronogolf calls the "
                                         "club's courses Gold/Blue/Red — which "
                                         "is Heritage is unconfirmed"),
    "wigwam-golf-club-patriot-course": ("needs_ids", "chronogolf calls the "
                                        "club's courses Gold/Blue/Red — which "
                                        "is Patriot is unconfirmed"),
}

# adapters that can actually fetch today
IMPLEMENTED = {"foreup", "teeitup", "chronogolf", "clubprophet", "clubcaddie",
               "membersports", "quick18", "teesnap", "foretees",
               "golfwithaccess", "totale", "rguest", "courseco",
               "teequest", "clubessential"}


def slugify(name: str) -> str:
    # Drop descriptive parentheticals like "(Sun City Grand)", "(fka ...)",
    # "(North & South)" so a course's slug stays stable when the directory adds
    # or edits a suffix. The full name is preserved in the "name" field.
    name = re.sub(r"\([^)]*\)", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_ids(platform: str, url: str) -> dict:
    m = PATTERNS.get(platform, re.compile(r"$^")).search(url or "")
    if not m:
        return {}
    g = m.groups()
    if platform == "foreup":
        return {"course_id": g[0], "schedule_id": g[1]}
    if platform == "teeitup":
        ids = {"alias": g[0]}
        # shared-tenant aliases (Phoenix muni, etc.) select a course via
        # ?course=<facilityId>; capture it so the adapter filters to it.
        cm = re.search(r"[?&]course=(\d+)", url or "")
        if cm:
            ids["facility_id"] = cm.group(1)
        return ids
    if platform == "clubprophet":
        return {"tenant": g[0]}
    if platform == "chronogolf":
        key = "club_id" if g[0].isdigit() else "slug"
        return {key: g[0], "club_uuid": None}
    if platform == "clubcaddie":
        return {"shard": g[0], "view_token": g[1]}
    if platform == "membersports":
        ids = {"club_id": g[0], "secondary_id": g[1]}
        # tee-times/{club}/{course}/{group}/{cfg}/{sheetType}: the 4th segment
        # is configurationTypeId, which selects WHICH tee sheet. The adapter
        # used to pin it to 0 and so lost every sheet only reachable at another
        # value, so carry it through and sweep it alongside 0.
        if len(g) > 3 and g[3] and g[3] != "0":
            ids["config_ids"] = [0, int(g[3])]
        return ids
    if platform == "ezlinks":
        return {"portal": g[0]}
    if platform == "golfnow":
        return {"golfnow_facility_id": g[0], "golfnow_slug": g[1].removesuffix("/search")}
    if platform in ("teesnap", "quick18"):
        ids = {"subdomain": g[0]}
        if "play18.com" in (url or ""):
            ids["domain"] = "play18.com"   # Quick18's newer domain
        return ids
    if platform == "noteefy":
        return {"venue_guid": g[0]}
    if platform == "foretees":
        return {"club_key": g[0], "cid": g[1]}
    if platform == "supersaas":
        return {"account": g[0], "schedule": g[1]}
    if platform == "courseco":
        # <tenant>.totaleintegrated.net/web/tee-times. The tenant is the ONLY
        # thing the gateway uses to decide whose sheet you get (it reads the
        # Origin header, not any parameter), so it is the whole id for a
        # single-course club. A tenant fronting several courses labels each
        # row from its own Courses list; two venues sharing one tenant pin an
        # extra course_id in EXTRA_IDS.
        return {"tenant": g[0]}
    if platform == "rguest":
        # book.rguest.com/onecart/golf/courses/<tenant>/<property>. A property
        # with several courses (We-Ko-Pa, Camelback) needs nothing more — the
        # adapter fetches and labels every course on it. Wildfire is the other
        # shape: two registry venues on ONE property, so those pin an extra
        # course_id in EXTRA_IDS to claim a single sheet each.
        return {"tenant": g[0], "property": g[1]}
    if platform == "teequest":
        sub = g[0]
        return {"site": g[1],
                "host": f"{sub}.teequest.com",
                "skin": "v2" if sub == "bookateetime" else "legacy"}
    return {}


# Source CSVs, each tagged with the state they cover. course_slug is the D1
# key, so slugs must be globally unique across states (collisions get a state
# suffix below).
SOURCES = [
    ("colorado_golf_courses_booking.csv", "CO"),
    ("arizona_golf_courses_booking.csv", "AZ"),
    ("virginia_golf_courses_booking.csv", "VA"),
]


def _course_from_row(row: dict, state: str, slug: str, venue_id: str,
                     source_role: str) -> dict:
    platform = row["Booking Platform"]
    ids = extract_ids(platform, row["Booking URL"])
    name_key = re.sub(r"\([^)]*\)", "", row["Course Name"]).strip().lower()
    ids.update(EXTRA_IDS.get(name_key, EXTRA_IDS.get(row["Course Name"].lower(), {})))
    if platform.startswith("other:"):
        status = "unsupported"
    elif platform not in IMPLEMENTED:
        status = "experimental"          # golfnow / ezlinks
    elif slug in PROBED_HOLDS:
        status = PROBED_HOLDS[slug][0]
    elif platform == "foreup" and not ids.get("course_id"):
        # Was `not ids.get("schedule_id")`, which held 15 courses out of the
        # scrape for an id the scrape finds by itself: ForeUpAdapter.fetch()
        # calls discover_ids(course_id) and regexes schedule_id out of the
        # booking page whenever the registry has not pinned one. Probed
        # 2026-07-26 — 13 of the 15 handed over a schedule_id on the first ask
        # (probe-results/needs-ids.json). course_id is the id fetch() genuinely
        # cannot work without, so that is what this now requires.
        status = "needs_ids"
    elif platform == "chronogolf" and not (ids.get("club_id") or ids.get("slug")):
        # Was `not ids.get("club_uuid")`, which rejected 100% of chronogolf
        # rows — extract_ids() hardcodes club_uuid=None on every one of them,
        # and no code anywhere reads club_uuid. ChronogolfAdapter.fetch() takes
        # `club_id or slug` and resolves the club id, affiliation type and
        # course ids at runtime via discover(). Thirty courses sat in needs_ids
        # for months waiting on an identifier that was never going to arrive
        # and was never needed. Probed 2026-07-26: 22 of the 30 resolved
        # cleanly; the other 8 are unclaimed listings, handled in the CSVs.
        status = "needs_ids"
    elif platform == "teeitup" and not ids.get("alias"):
        status = "needs_ids"             # e.g. Troon wrapper, no direct alias
    elif platform == "clubprophet" and not ids.get("tenant"):
        # The adapter mints its anonymous token from <tenant>.cps.golf. Lake
        # Arbor's portal is secure.west.prophetservices.com instead, where every
        # route redirects to a Log On page (probe-results/diag3.txt section D),
        # so there is no anonymous tee sheet to read. It was sitting at "ready"
        # with empty ids, i.e. failing on every single scrape.
        status = "unsupported"
    elif platform == "clubprophet" and not (ids.get("website_id")
                                            and ids.get("course_ids")):
        # A tenant alone is not enough to scrape: browser_cps.run() only
        # fetches rows that have tenant AND website_id AND course_ids, so a
        # tenant-only row is silently skipped on every scrape while the
        # registry calls it "ready". Emerald Greens and DU Highlands Ranch sat
        # there for months and had literally never been attempted. A browser
        # probe then found neither tenant is even live — emeraldgreens.cps.golf
        # answers 404 on the token endpoint and every DU candidate fails DNS,
        # against a control (indianpeaks) that mints a token fine
        # (probe-results/open_leads.txt section B). "needs_ids" is the honest
        # status: the platform is right, the identifiers are not known.
        status = "needs_ids"
    elif platform == "courseco" and not (ids.get("tenant") and ids.get("gateway")):
        # Both are required and neither is guessable. The gateway host decides
        # WHOSE sheet you get; the Origin header (built from the tenant) has to
        # agree with it or the gateway 400s. Measured 2026-07-28: the Sun City
        # West gateway answers 200 from the Sun City West origin and 400 from
        # Ken McDonald's, for a byte-identical request.
        status = "needs_ids"
    elif platform == "rguest" and not (ids.get("tenant") and ids.get("property")):
        # The adapter cannot address a sheet without both. Both come free from
        # the booking URL, so this only fires on a row whose URL is not a
        # book.rguest.com course link.
        status = "needs_ids"
    elif platform == "membersports" and not ids.get("club_id"):
        # A shared city portal can leave a course pointing at its neighbour's
        # golfClubId. Better to publish nothing than someone else's tee sheet.
        status = "needs_ids"
    elif platform == "teequest" and not ids.get("site"):
        # Both skins address a sheet by numeric site id, and both take it
        # straight out of the booking URL. This only fires on a row whose URL
        # is not a teequest link.
        status = "needs_ids"
    elif platform == "clubessential" and not (ids.get("host")
                                              and ids.get("course_ids")):
        # Nothing usable is in the booking URL — a ClubEssential club site is
        # a CMS page whose querystring names a CMS module, not a course. The
        # host and the GOLFCOURSE ids come out of the club's own widget call
        # and are pinned in EXTRA_IDS, so a row without them is not fetchable.
        status = "needs_ids"
    else:
        status = "ready"
    return {
        "slug": slug,
        # IDENTITY, not presentation. `name` feeds slugify() (so it must never
        # move — D1 rows are keyed on the slug) and it is what the adapters that
        # match by course name compare against. Golfer-facing text goes in
        # `display_name` instead, which is why the two are separate fields.
        "name": row["Course Name"],
        # Optional golfer-facing override, "Venue (Course)" for a facility whose
        # courses are separate venues. Empty for the ~97% of rows whose own name
        # already reads correctly.
        "display_name": (row.get("Display Name") or "").strip(),
        # "Ahwatukee (Phoenix)" -> "Ahwatukee": the parenthetical metro hint is
        # directory metadata; the clean primary place name is what the frontend
        # displays and matches against its city list.
        "city": re.sub(r"\s*\([^)]*\)", "", row["City"]).strip(),
        "state": state,
        # venue_id groups every booking SOURCE for one physical course. The
        # primary (native engine, or the only source) owns the clean venue slug;
        # supplemental sources (GolfNow overflow, ...) get a platform-suffixed
        # slug so course-scoped D1 sync never clobbers, but share this venue_id.
        "venue_id": venue_id,
        "source_role": source_role,
        "platform": platform,
        "booking_url": row["Booking URL"],
        "ids": ids,
        "status": status,
        "confidence": row["Confidence"],
        "notes": row["Notes"],
    }


def main() -> None:
    from collections import OrderedDict, Counter
    # Group rows into physical courses (venues). A venue is (state, base-slug);
    # most have one row, but a course with a native engine PLUS a GolfNow
    # overflow listing has two rows that must merge into one venue.
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for src, state in SOURCES:
        try:
            f = open(src)
        except FileNotFoundError:
            continue
        with f:
            for row in csv.DictReader(f):
                if row["Online Booking"] != "yes" or not row["Booking Platform"]:
                    continue
                vb = slugify(row["Course Name"])
                groups.setdefault((state, vb), []).append(row)

    courses = []
    taken: set = set()
    multi = 0
    for (state, vb), rows in groups.items():
        # Native engine(s) first, GolfNow last, so the native source becomes the
        # primary (canonical booking link + full inventory) and GolfNow — which
        # only carries a course's overflow — is a deduped supplement.
        rows_sorted = sorted(rows, key=lambda r: r["Booking Platform"] == "golfnow")
        base = vb if vb not in taken else f"{vb}-{state.lower()}"
        while base in taken:             # extremely rare same-state base clash
            base += "-x"
        venue_id = base
        if len(rows_sorted) > 1:
            multi += 1
        for idx, row in enumerate(rows_sorted):
            if idx == 0:
                slug, role = base, "primary"
            else:
                plat = row["Booking Platform"].split(":")[0]
                slug, role = f"{base}-{plat}", "supplement"
                while slug in taken:
                    slug += "-x"
            taken.add(slug)
            courses.append(_course_from_row(row, state, slug, venue_id, role))

    with open(OUT, "w") as f:
        json.dump({"generated_from": [s for s, _ in SOURCES], "courses": courses}, f,
                  indent=1)
    print(f"wrote {OUT}: {len(courses)} booking sources across "
          f"{len(groups)} venues ({multi} multi-source)")
    print("by state:", dict(Counter(c["state"] for c in courses)))
    print("by status:", dict(Counter(c["status"] for c in courses)))
    print("by source_role:", dict(Counter(c["source_role"] for c in courses)))


if __name__ == "__main__":
    main()
