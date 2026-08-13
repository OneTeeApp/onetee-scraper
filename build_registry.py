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
    # play.teeitup.GOLF joined the family with Florida (stoneybrook-east,
    # palmetto-pine) — same kenna backend, same alias semantics as .com.
    "teeitup": re.compile(r"https?://([a-z0-9-]+)\.(?:book(?:-v2)?\.teeitup\.(?:com|golf)|play\.teeitup\.(?:com|golf))"),
    "clubprophet": re.compile(r"https?://([a-z0-9]+)\.cps\.golf"),
    # Two shapes, one capture group. The marketplace link is /club/<slug>; the
    # embeddable widget a club pastes into its own site is
    # /en/club/<club_id>/widget?... — same club, but the locale segment made the
    # pattern miss and the row extracted nothing. extract_ids() already sorts
    # digits into club_id and words into slug, so allowing an optional two-letter
    # locale is the whole fix. Fore Sisters (MD) was the row that found this: it
    # sat at needs_ids with the club id sitting in plain sight in its URL.
    "chronogolf": re.compile(r"chronogolf\.(?:com|ca)/(?:[a-z]{2}/)?club/([a-z0-9-]+)"),
    "clubcaddie": re.compile(r"apimanager-(cc\d+)\.clubcaddie\.com/webapi/view/([a-z]+)"),
    "membersports": re.compile(r"app\.membersports\.com/(?:tee-times|book-tee-time|book-linked-clubs-tee-time|custom)/(\d+)/(\d+)(?:/(\d+))?(?:/(\d+))?"),
    # TenFore Golf portals live at fox.tenfore.golf/<vanity>. The vanity is
    # case-SENSITIVE in the wild (MD's "SligoCreek"), so allow capitals — the
    # same lesson rguest taught in Maryland. golfCourseID is NOT in the URL; it
    # is resolved from GetGolfCourseByVanity and pinned in EXTRA_IDS, so a row
    # without a pinned golf_course_id is held needs_ids below.
    "tenfore": re.compile(r"fox\.tenfore\.golf/([A-Za-z0-9-]+)"),
    "ezlinks": re.compile(r"https?://([a-z0-9-]+)\.ezlinks(?:golf)?\.com"),
    # teeoff.com is EZLinks' CONSUMER marketplace, not a course's own portal —
    # same URL shape as a GolfNow facility page, because GolfNow owns both. A
    # row carrying one of these has no <portal>.ezlinksgolf.com host, so the
    # ezlinks pattern above extracts nothing and browser_ezlinks skips it in
    # silence. Capturing the facility id here does NOT make such a row
    # scrapeable (no adapter reads teeoff yet); it preserves the identifier
    # that was already discovered so a future pass starts from it instead of
    # rediscovering it, while the status guard below keeps the row honest.
    "teeoff": re.compile(r"teeoff\.com/tee-times/facility/(\d+)-([a-z0-9-]+)"),
    "golfnow": re.compile(r"golfnow\.com/tee-times/facility/(\d+)-([a-z0-9-]+)"),
    "teesnap": re.compile(r"https?://([a-z0-9-]+)\.teesnap\.net"),
    "quick18": re.compile(r"https?://([a-z0-9-]+)\.(?:quick18|play18)\.com"),
    "noteefy": re.compile(r"booking\.noteefy\.app/e/([0-9a-f-]+)"),
    "foretees": re.compile(r"foretees\.com/.*clubKey=([A-Za-z0-9]+)&cid=(\d+)"),
    "supersaas": re.compile(r"supersaas\.com/schedule/([^/]+)/([^/?#]+)"),
    # Property is case-SENSITIVE and often camelCase ("RockyGapBook"), so the
    # character class must allow capitals. It was [a-z0-9-] until Maryland
    # arrived, which silently dropped Rocky Gap to needs_ids: the pattern did
    # not match, so no tenant/property was extracted and nothing said why.
    "rguest": re.compile(r"book\.rguest\.com/onecart/golf/courses/(\d+)/([A-Za-z0-9-]+)"),
    # Agilysys OneCart: same product/API as rguest, different host
    # (book.onagilysys.com, e.g. Black Desert). Captures tenant + property.
    "agilysys": re.compile(r"book\.onagilysys\.com/onecart/golf/courses/(\d+)/([A-Za-z0-9-]+)"),
    # GolfPay: golfpay.co/course/<slug>. course_id + tsid are NOT in the URL
    # (read off the page's /api/tee-times call) and pinned in EXTRA_IDS.
    "golfpay": re.compile(r"golfpay\.co/course/([a-z0-9-]+)"),
    # EasyTee: app.easyteegolf.com/course/<slug>/ (slug may contain an
    # apostrophe, e.g. schneiter's-pebblebrook-golf-club).
    "easytee": re.compile(r"app\.easyteegolf\.com/course/([^/?#]+)"),
    # GolfRev is Cybergolf's tee-time engine. A Cybergolf course's own site
    # (e.g. birchcreekgolf.com) links out to golfrev.com/go/tee_times/ carrying
    # both ids in the query string: ?htc=<h>&courseid=<c> (order varies, so
    # extract_ids pulls each independently rather than positionally).
    "golfrev": re.compile(r"golfrev\.com/go/tee_times/"),
    # Golfscape: golfscape.com/<region>/<course-slug>. The numeric propertyId the
    # /executeaction API needs is NOT in the URL (nor in the embed courseCode), so
    # the slug is documentation only and property_id is pinned in EXTRA_IDS.
    "golfscape": re.compile(r"golfscape\.com/[a-z0-9-]+/([a-z0-9-]+)"),
    # Trutee: trutee.app/courses/o/<org> — the org portal lists every course
    # under that org; per-venue attribution is by the pinned trutee_course name
    # (browser_trutee.py). Captures the org slug.
    "trutee": re.compile(r"trutee\.app/courses/o/([a-z0-9-]+)"),
    "courseco": re.compile(r"https?://([a-z0-9-]+)\.totaleintegrated\.net"),
    # GolfBack puts the course uuid in the SPA fragment, so every row carries
    # its own id and nothing needs pinning. The trailing slash is optional.
    "golfback": re.compile(r"golfback\.com/#/course/([0-9a-f]{8}-[0-9a-f-]{27})"),
    # TeeQuest ships two skins. Legacy is teetimes.teequest.com/<site>; v2 is
    # bookateetime.teequest.com/course/<site>. Same operator, different
    # request shape, so the host is captured alongside the id.
    "teequest": re.compile(
        r"https?://(teetimes|bookateetime)\.teequest\.com/(?:course/)?(\d+)"),
    # Golf With Access is Troon's public booking platform, and a
    # troon.com/course/<tenant>/reserve-tee-time page fronts the same widget
    # under the same tenant slug, so both URL shapes yield the tenant. The
    # bookable course uuid is NOT in any URL (see the adapter's hazard notes) —
    # it comes from EXTRA_IDS after a probe, so a URL-only row sits needs_ids.
    "golfwithaccess": re.compile(
        r"(?:golfwithaccess|troon)\.com/course/([a-z0-9-]+)"),
}

# extra IDs known from research that aren't visible in the URL
# TenFore golfCourseID by vanity slug (resolved via GetGolfCourseByVanity).
# Keyed by the URL vanity, NOT the course name: several MCG courses (Falls Road,
# Needwood, ...) exist BOTH as a Montgomery County chronogolf portal course and
# as their own TenFore portal, so a name key would collide with the chronogolf
# course_ids pinned in EXTRA_IDS. Vanity is globally unique per course.
TENFORE_IDS = {
    "colonialhills": "16527", "bluesky": "16532", "hampshiregreens": "16506",
    "littlebennett": "16508", "needwood": "16509", "laytonsville": "16507",
    "crossvines": "16510", "fallsroad": "16503", "northwest": "16504",
    "sligocreek": "16512", "rattlewood": "16511",
    # Utah (resolved 2026-08-07 via GetGolfCourseByVanity, name-confirmed):
    "theranches": "16515", "coralcanyon": "16516",
    # Vermont (resolved 2026-08-13 via GetGolfCourseByVanity; vanity
    # "Woodstockresort" answers with name "Woodstock Inn & Resort"):
    "woodstockresort": "16581",
}

EXTRA_IDS = {
    # Trutee (City of St. George org) — each venue's EXACT trutee.app img-alt
    # course name, matched verbatim by browser_trutee.py (Trutee drops our
    # "Dixie" prefix and calls Southgate a "Course", so these are pinned, not
    # fuzzy-matched).
    "dixie red hills golf course": {"trutee_course": "Red Hills Golf Course"},
    "southgate golf club": {"trutee_course": "Southgate Golf Course"},
    "st. george golf club": {"trutee_course": "St. George Golf Club"},
    "sunbrook golf club": {"trutee_course": "Sunbrook Golf Club"},
    # GolfPay: The Barn (UT) — course_id/tsid read off golfpay.co 2026-08-08.
    "the barn golf club": {"course_id": 1466, "tsid": 20},
    # Golfscape: Copper Rock (UT) — numeric propertyId 3713, read off the course
    # page's booking-box-fetch-teetimes call 2026-08-10 (the embed courseCode is
    # 125e71; neither number is in a public URL). Its own /book-tee-times page
    # only links out to the golfscape marketplace, so this is the sole channel.
    "copper rock golf course": {"property_id": "3713"},
    # Sand Hollow Resort (UT): two OneTee venues on chronogolf club
    # sand-hollow-resort (14225). Pin each to its own course id so neither
    # publishes the other's sheet (confirmed off /private_api 2026-08-07:
    # Championship=16313, The Links=23670; a 9-hole "Championship Back 9"
    # 27620 is left out of the 18-hole Championship venue on purpose).
    "sand hollow resort - championship course": {"course_ids": [16313]},
    "sand hollow resort - links course": {"course_ids": [23670]},
    # Star Valley Ranch (WY): Cedar Creek (18) and Aspen Hills (9) are two
    # OneTee venues on chronogolf club star-valley-ranch (15494), which also
    # carries a "Simulator at Cedar Creek" course (27877). Pin each venue to
    # its own course id so neither publishes the other's sheet or the
    # simulator's empty one (read off /private_api 2026-08-13).
    "cedar creek golf course": {"course_ids": [17793]},
    "aspen hills golf course": {"course_ids": [23538]},

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

    # Heather Gardens (HOA course, Aurora CO): its window.courses TOP-LEVEL id
    # (148) is an EMPTY placeholder sheet, so the adapter's default discovery
    # returned 0 on every date. The real public tee sheet lives under
    # teetimes-day course 131 (verified live 2026-08-04 from heathergardens.
    # teesnap.net: 78 Wed slots at $42/$32). teetimes-day resolves course ids
    # globally, so pinning 131 for this tenant is valid even though 131 is not in
    # heathergardens' own window.courses; the teesnap adapter now trusts explicit
    # pins (no other teesnap row is pinned, so this changes nothing else).
    "heather gardens golf course": {"teesnap_course_ids": [131]},

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
    # Four FL Club Prophet tenants, ids captured 2026-08-06 via the same
    # anonymous flow on each tenant's own origin (post-Cloudflare, in a real
    # browser). Highlands Ridge N/S share ONE tenant (highlandsridgefl) and were
    # both unpinned, so the plain flow would have published the combined sheet
    # under both slugs — these pins are what let the tenant move to the free
    # datacenter-direct set in browser_cps. Oriole confirmed serving (41 slots,
    # Sat 2026-08-08). Crane Lakes' GetAllOptions returns EMPTY courseOptions,
    # so discovery can never pin it — but courseIds=1 with this websiteId
    # returns the real sheet (67 slots, Sat 2026-08-08, "Crane Lakes" label).
    "highlands ridge golf club - north course":
        {"website_id": "dfc45eee-2f31-4681-997d-08db8480dd15", "course_ids": [1]},
    "highlands ridge golf club - south course":
        {"website_id": "dfc45eee-2f31-4681-997d-08db8480dd15", "course_ids": [2]},
    "oriole golf club":
        {"website_id": "18dda4e6-5af0-4b47-ef1e-08daa629da2f", "course_ids": [1]},
    "crane lakes golf & country club":
        {"website_id": "f197d475-3ab5-47d9-ded6-08db675c16b2", "course_ids": [1]},

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

    # TeeItUp facility_id PINS (2026-08-10). These 11 courses read zero for
    # weeks — the coverage/landed-zero report flagged them. Their kenna ALIAS is
    # fine, but the registry facility_id was NULL (the booking URL had no
    # ?course=<n>) or WRONG (emerald-bay's URL is ?course=54f14d... — a Mongo
    # courseId whose leading digits "54" were mis-read as the facility id; the
    # real facility is 4897). With no/ wrong pin, fetch() depends on runtime
    # facility discovery, which is unreliable through the throttled kenna proxy,
    # so the course errors and never stamps fresh. Each id below was read live
    # from phx-api-be-east-1b.kenna.io/alias/<alias>/facilities (all return
    # inventory). A correct pin skips the discovery hop — see the fetch() note in
    # adapters/teeitup.py (~L423). (Not pinned: seven-springs CHAMPION already
    # has the right id 5782 yet reads empty — a real thin sheet, not this bug;
    # mainlands-golf-course + the-preserve-18-hole-championship-golf-course
    # aliases 404 at kenna = delisted, need a fresh alias.)
    "arlington ridge golf club":                            {"facility_id": "2102"},
    "eagle springs golf course":                            {"facility_id": "2096"},
    "emerald bay golf club":                                {"facility_id": "4897"},
    "highlands reserve golf club":                          {"facility_id": "2240"},
    "palmetto-pine country club":                           {"facility_id": "5534"},
    "polo park golf course":                                {"facility_id": "20055"},
    "river run golf club":                                  {"facility_id": "950"},
    "serenoa golf club":                                    {"facility_id": "2697"},
    "seven springs golf & country club - executive course": {"facility_id": "6280"},
    "shalimar pointe golf club":                            {"facility_id": "268"},
    "torres blancas golf club":                             {"facility_id": "872"},
    # Two more where the registry's ALIAS 404s at kenna (the pinned facility_id
    # is already correct — 1710 / 3664). Real alias read from kenna 2026-08-10:
    # mainlands-golf-course -> mainlands-golf-club (facility 1710); the-preserve-
    # 18-hole-championship-golf-course -> saltleaf-golf-preserve (facility 3664).
    "mainlands golf club":                          {"alias": "mainlands-golf-club"},
    "saltleaf golf preserve - the preserve course": {"alias": "saltleaf-golf-preserve"},

    # TeeItUp needs_ids ALIAS+FACILITY discovery (2026-08-10, run 31409654256).
    # These vanity booking hosts are NOT the kenna alias, so /alias/<vanity>/
    # facilities 404s and each row sat at needs_ids (PROBED_HOLDS below). The
    # booking PAGE, though, loads the real tenant and fires /v2/tee-times to
    # phx-api-be-east-1b.kenna.io with the REAL alias in its x-be-alias HEADER.
    # probe_teeitup_alias.py drove each page in Chromium and read that header +
    # facilityIds off the outgoing request. Pinning both alias and facility_id
    # here (EXTRA_IDS wins over the URL-derived vanity alias) flips them to ready.
    # winter-park-pines is the same fix in disguise: its old alias
    # winter-park-pines-golf-club-wp18 knew the tenant but 404'd facility 5634 —
    # the REAL tenant is winter-pines-golf-club, which owns 5634. (stonecrest
    # fired no kenna call — no alias captured — so it stays needs_ids.)
    "clermont national golf club":               {"alias": "sanctuary-ridge-golf-club", "facility_id": "1499"},
    "little sandy at omni amelia island resort": {"alias": "omni-amelia-island-plantation-ocean-links-course", "facility_id": "4487"},
    "water oak golf club":                       {"alias": "water-oak-9-hole", "facility_id": "2689"},
    "wildcat crossing golf club":                {"alias": "majestic-golf-club", "facility_id": "8142"},
    "legacy golf club":                          {"alias": "holiday-golf-club", "facility_id": "4751"},
    "sebring international golf resort":          {"alias": "sebring-international-golf-resort-panther-creek", "facility_id": "2378"},
    "willow lakes golf club":                    {"alias": "willow-lakes-rv-park-and-golf-resort", "facility_id": "10700"},
    "capri isles golf club":                     {"alias": "the-golf-club-at-capri-isles", "facility_id": "4092"},
    "stoneybrook west golf club":                {"alias": "stoneybrook-west", "facility_id": "1943"},
    "winter park pines golf course":             {"alias": "winter-pines-golf-club", "facility_id": "5634"},

    # Maryland shared Chronogolf portals. Eleven Montgomery County courses and
    # both Turf Valley courses answer on ONE club slug each, so without a pinned
    # course_id the adapter fetches every course on the club and publishes the
    # whole portal's sheet under each venue's name. Read off
    # /private_api/clubs/<slug>/courses on 2026-07-29; the MCG list also carries
    # a decoy row, id 21186 "DO NOT USE - PLEASE PICK YOUR COURSE", which is why
    # this is pinned rather than filtered by name.
    "hampshire greens golf course":      {"course_ids": [21183]},
    "little bennett golf course":        {"course_ids": [21181]},
    "needwood golf course":              {"course_ids": [21180]},
    "needwood golf course - executive 9": {"course_ids": [21179]},
    "laytonsville golf course":          {"course_ids": [21182]},
    "poolesville golf course":           {"course_ids": [21176]},   # "Crossvines" in MCG
    "falls road golf course":            {"course_ids": [21184]},
    "northwest golf course":             {"course_ids": [21178]},
    "northwest golf course - inside 9":  {"course_ids": [21177]},
    "sligo creek golf course":           {"course_ids": [21174]},
    "rattlewood golf course":            {"course_ids": [21175]},
    "turf valley resort - original course": {"course_ids": [8698]},
    "turf valley resort - hialeah course":  {"course_ids": [8697]},
    # Queenstown Harbor: one Chronogolf club (7597) fronting both courses, so
    # each venue must claim exactly one sheet or the two publish each other's
    # tee times. Verified live 2026-07-30: River 8665 (70 times, from $155),
    # Lakes 8664 (74 times). The club also lists LBI National and Vineyard
    # National with online booking disabled — out-of-state courses on the same
    # tenant, which is the second reason not to leave these rows unpinned.
    "queenstown harbor - river course":  {"course_ids": [8665]},
    "queenstown harbor - lakes course":  {"course_ids": [8664]},
    # Single bookable course, pinned anyway: club 7630 also carries a "UMD Sim
    # Room" sheet with online booking disabled today. The adapter filters on
    # that flag, so an unpinned row works right now — until someone enables the
    # sim room and the course starts publishing indoor bays as golf.
    "university of maryland golf course": {"course_ids": [8701]},
    # Fore Sisters pins its course id because the one in its own booking URL is
    # WRONG: the widget link ends #?course_id=21287, and club 19031's live course
    # list has exactly one entry, 23386. Asking for 21287 gets a 422, so a future
    # pass that "helpfully" reads the fragment would break a working course.
    # Measured 2026-07-30: 58 times on each of 07-30, 07-31, 08-02 and 08-04
    # (08-01 empty, 08-06 onward 422 — the club books about a week out).
    "fore sisters golf course": {"course_ids": [23386]},

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

    # Club Prophet is a browser-owned tier and browser_cps.run() skips any row
    # missing website_id or course_ids, so a tenant-only row is silently never
    # attempted. Pinned from the tenant's own GetAllOptions on 2026-07-28: one
    # course, id 1, named "Westfields Golf Club" — so there is no sibling on
    # this tenant to publish by mistake.
    "westfields golf club": {"website_id": "cc6fdbac-e37d-4235-e448-08d9bbe6c02f",
                             "course_ids": [1]},

    # ResortSuite. The booking URL is a bare SPA route with nothing in it, and
    # the ONLY thing separating the resort's two courses is the three-letter
    # CourseId inside the SOAP body. Verified 2026-07-29 by driving CourseId
    # directly: CAS answers 54 times on 10-minute intervals, OLD answers 68 on
    # 8-minute intervals for the same date. The Old Course had been held as
    # "unproven" because the SPA ignores its own route and re-sent CAS when you
    # clicked through to it — an artifact of the page, not the service.
    "the omni homestead resort - cascades course": {
        "host": "omnihomesteadexperiences.com", "course_id": "CAS"},
    "the omni homestead resort - old course": {
        "host": "omnihomesteadexperiences.com", "course_id": "OLD"},

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
    # Three Utah ForeUp munis, same class of bug (2026-08-10). All were "ready"
    # but reading zero: the times endpoint returns [] with no booking_class, and
    # discover_ids() can't find one because these courses only expose the class
    # after you pick a booking TYPE in the SPA (18 Hole Groups / Public
    # Reservations), not in the page HTML. Captured each live off the SPA's own
    # /api/booking/times request, then verified the class returns inventory:
    #   Crane Field  bc 6420 (schedule 1)     -> ~60-72 times/day
    #   Davis Park   bc 2094 (schedule 1757)  -> short 2-3 day window, a few/day
    #   Valley View  bc 1208 (schedule 1759)  -> 15-20 on open days (had NO
    #                schedule_id pinned before, so also pin 1759).
    "crane field golf course":   {"booking_class": "6420"},
    "davis park golf course":    {"booking_class": "2094"},
    "valley view golf course":   {"booking_class": "1208", "schedule_id": "1759"},
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
    # (mariana butte is pinned once, above with the discover3 batch — a second
    #  copy here once shadowed it as a duplicate dict key)
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

    # Green Hill CC (MD) is NetCaddy/ClubEssential on the club's own host, not
    # a bespoke widget. Verified 2026-07-30 against the adapter's exact endpoint,
    # /a_master/net/netcaddy/api/teetimes/Available: SiteID 4614, CourseId 1,
    # 29-69 rows a day at $58-$88, and the sheet answers for exactly the day
    # asked for. Nothing new to build - the row was simply tagged other:*.
    "green hill country club": {"host": "www.greenhillcc.com", "site_id": 4614,
                                "course_ids": [1]},

    # --- Maryland cps.golf tenants (GetAllOptions, 2026-07-30) ---------------
    #
    # Read GetAllOptions, NOT Home/Configuration. Every one of these tenants
    # reports websiteId 00000000-0000-0000-0000-000000000000 at
    # /onlineresweb/Home/Configuration, and OnlineCourses answers that zero GUID
    # with HTTP 200 and an EMPTY course list — so a tenant that sells 250 tee
    # times a day looks like a tenant with no courses, and nothing errors.
    # GetAllOptions/<tenant> returns the real webSiteId plus courseOptions.
    #
    # Ruark Golf runs FIVE courses on ONE site (websiteId 7b7ca697...), which is
    # what Brian meant by "one portal for 5 courses". Each course keeps its own
    # <tenant>.cps.golf booking URL because that is what a golfer should be sent
    # to, and each was verified to serve its OWN sheet from its OWN host with
    # this shared websiteId: lighthousesound/2=45, manowar/3=50,
    # nutterscrossing/4=67, rumpointe/5=42, waradmiral/6=49 on 2026-07-31.
    "glenriddle - man o' war":       {"website_id": "7b7ca697-54ee-4d4f-bc04-08dadc553eee", "course_ids": [3]},
    "glenriddle - war admiral":      {"website_id": "7b7ca697-54ee-4d4f-bc04-08dadc553eee", "course_ids": [6]},
    "links at lighthouse sound":     {"website_id": "7b7ca697-54ee-4d4f-bc04-08dadc553eee", "course_ids": [2]},
    "nutters crossing golf club":    {"website_id": "7b7ca697-54ee-4d4f-bc04-08dadc553eee", "course_ids": [4]},
    "rum pointe seaside golf links": {"website_id": "7b7ca697-54ee-4d4f-bc04-08dadc553eee", "course_ids": [5]},
    # Ocean City Golf Club: one tenant, two courses, verified 45 and 50 times.
    "ocean city golf club - newport bay": {"website_id": "273a2a31-c18a-460e-e8af-08dd18610ede", "course_ids": [1]},
    "ocean city golf club - seaside":     {"website_id": "273a2a31-c18a-460e-e8af-08dd18610ede", "course_ids": [2]},
    # Eagle's Landing left Chronogolf for its own CPS tenant; 27 times verified.
    "eagle's landing golf course":   {"website_id": "385ada1f-3d2f-47a3-33bb-08dd0d75edeb", "course_ids": [1]},

    # --- Baltimore County Golf: five schedules under one foreUp course -------
    #
    # `schedule_name` is the field that identifies these, and it disagrees with
    # `course_name` on every row. course_name says "Baltimore County Golf" or
    # "Diamond Ridge & The Woodlands"; schedule_name says which course it
    # actually is. Reading course_name is what left this portal unresolved for
    # two sessions, including a written conclusion that Diamond Ridge and The
    # Woodlands could not be separated at all. They separate cleanly:
    #   4168 Greystone   4169 Diamond Ridge   4170 Fox Hollow
    #   4171 Rocky Point 4177 The Woodlands
    # Each schedule id lives in its own row's booking URL. What has to be pinned
    # is the booking class: this portal 401s for every class except 38 ("General
    # Public"), and 400s with none at all, so an unpinned row cannot read a
    # single tee time. Verified 2026-07-31 with real prices ($28-$48).
    "greystone golf course":     {"booking_class": "38"},
    "diamond ridge golf course": {"booking_class": "38"},
    "fox hollow golf course":    {"booking_class": "38"},
    "rocky point golf course":   {"booking_class": "38"},
    "the woodlands golf course": {"booking_class": "38"},
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
    # Two Florida TeeItUp rows whose alias kenna has never heard of: the real
    # production fetch path answered 404 on /v2/courses for both, on every date
    # probed (probe-results/newly-ready.json, 2026-07-29). That is the Golden
    # Hills signature — a vanity booking host that serves a real tenant page
    # while the API namespace does not contain the alias — and it means these
    # sat at `ready` erroring on every single scrape.
    #
    # Held rather than repaired because repairing needs the alias read off each
    # club's own /alias/ network call, and guessing one is how Viniterra ended
    # up on a REAL but wrong tenant that answered 200 with zero rows for weeks
    # (94d655b). needs_ids is the honest status: platform right, alias unknown.
    # RESOLVED 2026-08-10 (run 31409654256): probe_teeitup_alias.py drove each
    # vanity booking page in Chromium and read the REAL x-be-alias header off its
    # own /v2/tee-times request — the alias the /alias/<vanity>/ 404 was hiding.
    # Ten of these eleven kenna-404 holds now carry a correct alias+facility_id
    # pin in EXTRA_IDS above and are promoted to ready; only stonecrest is kept
    # (its booking page fired no kenna call, so no alias was captured).
    "stonecrest-golf-club":
        ("needs_ids", "kenna 404s alias broad-stripes-golf-club-at-stonecrest "
                      "on /v2/courses, all dates (club rebranded to Broad "
                      "Stripes; the new vanity host is not a kenna alias); "
                      "2026-08-10 booking page fired no kenna call — no alias"),
    # ForeUp booking page 22056 loads but exposes no schedule_id, so
    # discover_ids() returns nothing and fetch() has nothing to query — the
    # same shape as meeker and snowflake above.
    "rocky-bayou-country-club": ("needs_ids", "foreup booking page 22056 "
                                 "exposes no schedule_id"),
    # River Run (MD) used to be held here: chronogolf club 19908 resolves but its
    # only course, 28443, has online_booking_enabled=false, re-verified live
    # 2026-07-30. The hold is retired rather than kept because the CSV no longer
    # sends it to chronogolf at all — Brian's 2026-07-30 revision moved it to
    # TeeItUp alias river-run-golf-club, measured at 62-82 times/day on facility
    # 950. A status override that names a platform the row has left is a trap:
    # it would pin the venue at needs_ids no matter how well the new engine
    # works, and the comment would explain a fault that no longer exists.
}

# adapters that can actually fetch today
IMPLEMENTED = {"foreup", "teeitup", "chronogolf", "clubprophet", "clubcaddie",
               "membersports", "quick18", "teesnap", "foretees",
               "golfwithaccess", "totale", "rguest", "courseco",
               "teequest", "clubessential", "resortsuite", "golfback",
               "tenfore", "agilysys", "golfpay", "easytee", "golfrev",
               "golfscape"}


def slugify(name: str) -> str:
    # Drop descriptive parentheticals like "(Sun City Grand)", "(fka ...)",
    # "(North & South)" so a course's slug stays stable when the directory adds
    # or edits a suffix. The full name is preserved in the "name" field.
    name = re.sub(r"\([^)]*\)", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_ids(platform: str, url: str) -> dict:
    # Checked before the platform's own pattern because a teeoff.com link
    # NEVER matches it: four Florida rows tagged ezlinks carry a teeoff
    # marketplace URL instead of a <portal>.ezlinksgolf.com host, so they
    # extracted {} and sat at `experimental`, which browser_ezlinks skips in
    # silence on every run. Keeping the facility id costs nothing and stops the
    # next pass rediscovering it; the status guard still routes them to
    # needs_ids, because no adapter can read teeoff today.
    tm = PATTERNS["teeoff"].search(url or "")
    if tm and platform in ("ezlinks", "golfnow"):
        return {"teeoff_facility_id": tm.group(1),
                "teeoff_slug": tm.group(2).removesuffix("/search")}
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
    if platform == "golfback":
        return {"course_uuid": g[0]}
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
    if platform == "agilysys":
        # Same onecart shape as rguest; pin the host so RGuestAdapter targets
        # book.onagilysys.com instead of book.rguest.com.
        return {"tenant": g[0], "property": g[1], "host": "book.onagilysys.com"}
    if platform == "golfpay":
        # slug is documentation; course_id + tsid come from EXTRA_IDS.
        return {"slug": g[0]}
    if platform == "easytee":
        return {"slug": g[0]}
    if platform == "golfscape":
        # slug is documentation; the numeric property_id comes from EXTRA_IDS.
        return {"slug": g[0]}
    if platform == "trutee":
        return {"org": g[0]}
    if platform == "golfrev":
        # Both ids live in the query string; order is not guaranteed, so read
        # each on its own instead of relying on capture-group position.
        ids: dict = {}
        cm = re.search(r"[?&]courseid=(\d+)", url or "")
        hm = re.search(r"[?&]htc=(\d+)", url or "")
        if cm:
            ids["courseid"] = cm.group(1)
        if hm:
            ids["htc"] = hm.group(1)
        return ids
    if platform == "teequest":
        sub = g[0]
        return {"site": g[1],
                "host": f"{sub}.teequest.com",
                "skin": "v2" if sub == "bookateetime" else "legacy"}
    if platform == "golfwithaccess":
        return {"tenant": g[0]}
    if platform == "tenfore":
        # golf_course_id keyed by vanity (globally unique), not by name.
        v = g[0]
        ids = {"vanity": v}
        gid = TENFORE_IDS.get(v) or TENFORE_IDS.get(v.lower())
        if gid:
            ids["golf_course_id"] = gid
        return ids
    return {}


# Source CSVs, each tagged with the state they cover. course_slug is the D1
# key, so slugs must be globally unique across states (collisions get a state
# suffix below).
SOURCES = [
    ("colorado_golf_courses_booking.csv", "CO"),
    ("arizona_golf_courses_booking.csv", "AZ"),
    ("virginia_golf_courses_booking.csv", "VA"),
    ("florida_golf_courses_booking.csv", "FL"),
    ("maryland_golf_courses_booking.csv", "MD"),
    ("utah_golf_courses_booking.csv", "UT"),
    ("washington_dc_golf_courses_booking.csv", "DC"),
    ("alaska_golf_courses_booking.csv", "AK"),
    ("vermont_golf_courses_booking.csv", "VT"),
    ("wyoming_golf_courses_booking.csv", "WY"),
]


def _course_from_row(row: dict, state: str, slug: str, venue_id: str,
                     source_role: str) -> dict:
    platform = row["Booking Platform"]
    ids = extract_ids(platform, row["Booking URL"])
    name_key = re.sub(r"\([^)]*\)", "", row["Course Name"]).strip().lower()
    ids.update(EXTRA_IDS.get(name_key, EXTRA_IDS.get(row["Course Name"].lower(), {})))
    if platform.startswith("other:"):
        status = "unsupported"
    elif platform == "ezlinks" and not ids.get("portal"):
        # browser_ezlinks addresses a sheet by portal subdomain and nothing
        # else, so a row without one is skipped on every run while the registry
        # calls it `experimental` — a status that reads as "the browser tier
        # will get to it" and is therefore a quieter lie than needs_ids. Four
        # Florida rows (Crane Watch, Martin Downs Osprey Creek, Savanna Club,
        # Spruce Creek CC) carry teeoff.com marketplace links rather than a
        # portal host and had never been attempted.
        status = "needs_ids"
    elif platform == "tenfore" and not ids.get("golf_course_id"):
        # golfCourseID is resolved off GetGolfCourseByVanity and pinned in
        # EXTRA_IDS; without it the adapter cannot address a sheet.
        status = "needs_ids"
    elif platform == "golfpay" and not ids.get("course_id"):
        # golfpay's course_id/tsid are not in the URL; without the pinned
        # course_id the /api/tee-times call cannot be addressed.
        status = "needs_ids"
    elif platform == "golfrev" and not ids.get("courseid"):
        # golfrev keys the tee sheet on courseid; a row whose URL lacks it
        # (or isn't a golfrev tee_times link) cannot address a sheet.
        status = "needs_ids"
    elif platform == "golfscape" and not ids.get("property_id"):
        # golfscape's numeric propertyId is not in the URL (nor the embed
        # courseCode); without the pinned property_id the /executeaction call
        # cannot be addressed.
        status = "needs_ids"
    elif platform == "golfnow" and not ids.get("golfnow_facility_id"):
        # Same shape: browser_golfnow needs the numeric facility id. Lakeview
        # (FL) has an empty booking URL entirely, so there is nothing to
        # address and "experimental" overstated it.
        status = "needs_ids"
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
    elif platform == "golfback" and not ids.get("course_uuid"):
        # The uuid is the whole address and it is always in the booking URL, so
        # this only fires on a row whose URL is not a golfback.com course link.
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
    elif platform == "golfwithaccess" and not ids.get("course_id"):
        # The adapter refuses to fetch without the pinned course uuid (hazard 1
        # in its header: only the exact bookable course id returns rows, the
        # facility-named id is a dead aggregate, and a wrong id silently
        # succeeds with someone else's sheet). The uuid lives in the tenant
        # page's SSR courses[] array, never in the URL, so a row that has only
        # a tenant is honest work-to-do, not ready. Every CO/AZ/VA row already
        # pins its uuid in EXTRA_IDS; this fires on new-state rows (FL Troon
        # courses) until their probe runs.
        status = "needs_ids"
    elif platform == "totale" and not (ids.get("tenant") and ids.get("label")):
        # browser_totale only fetches rows carrying tenant AND label (the
        # exact course name its DNN sheet prints), and neither is in a booking
        # URL that points at a club's own CMS page. Without this guard such a
        # row reads "ready" while being silently skipped on every scrape — the
        # Emerald Greens pattern the clubprophet guard above documents.
        status = "needs_ids"
    elif platform == "resortsuite" and not (ids.get("host")
                                            and ids.get("course_id")):
        # Nothing is derivable from the booking URL — it is a bare SPA route.
        # CourseId is a three-letter course code (CAS, OLD) that only appears
        # inside the SOAP body, and it is the ONLY thing separating two courses
        # that share a host, so it is pinned in EXTRA_IDS per row.
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
                # A closed course cannot sell a tee time, so it has no business
                # in the registry no matter what its platform column still says.
                # build_directory.py drops Closed rows, so a closed row that
                # kept a platform became a registry venue with no card —
                # exactly what verify_directory.py flags as "would render live
                # with no card". The Colonial Golf Course (Lanexa) did this on
                # 2026-07-28: retyped Closed while still carrying `golfnow`.
                if (row.get("Type") or "").strip().lower() == "closed":
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
