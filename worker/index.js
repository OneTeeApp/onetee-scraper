/**
 * OneTee read API — a Cloudflare Worker in front of the D1 database.
 * Deploy with:  cd worker && npx wrangler deploy
 * Then your site can call e.g.
 *   GET https://onetee-api.<you>.workers.dev/api/tee-times?date=2026-07-25&state=CO&city=Denver&max_price=80
 *   GET .../api/courses?state=CO
 *   GET .../api/health
 *
 * VENUE MODEL: a physical course can have several booking SOURCES — a native
 * engine (foreUP/EZLinks/…) plus a GolfNow "overflow" listing. Each source is a
 * separate row-set keyed by its own course_slug, but they share a `venue_id`.
 * This API collapses sources to one venue everywhere: /api/courses groups by
 * venue_id, and /api/tee-times dedupes overlapping times (keeping the primary
 * source's booking link) and returns course_slug = venue_id, so the frontend
 * sees ONE course per venue with no changes needed.
 *
 * SUB-COURSES: multi-course facilities (Hyland Hills Gold/Blue/Par 3, Kennedy)
 * carry a per-slot `course_label`. Same-time slots on different sub-courses are
 * distinct rows, and course_name is rewritten to a display name that names the
 * sub-course, so the frontend differentiates them automatically.
 *
 * PAST TIMES: slots earlier than "now" in the course's local timezone are
 * hidden by default (they can't be booked). ?include_past=1 disables the filter.
 */

import DIRECTORY from "./directory.gen.js";
import REVALIDATE_IDS from "./revalidate-ids.gen.js";

const CORS = {
  "Access-Control-Allow-Origin": "*", // tighten to https://www.oneteeapp.com later
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  // Authorization MUST be allowed: the signed-in widget attaches a Clerk
  // bearer token, which makes the browser preflight. Omitting it fails the
  // preflight with a bare "TypeError: Failed to fetch" that says nothing
  // about headers (measured — see claude/membership-setup-clerk-stripe.md).
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

const json = (data, status = 200, extraHeaders = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS, ...extraHeaders },
  });

// "now" as a naive local ISO string in a tz, comparable to stored teetimes.
const localNowISO = (tz) =>
  new Date().toLocaleString("sv-SE", { timeZone: tz }).replace(" ", "T");

// State → IANA timezone, mirroring scraper/d1.py's _STATE_TZ. The registry is
// CO+AZ today, but hard-coding "AZ or else Denver" silently breaks the moment a
// third state lands: an Eastern course would be filtered against Mountain time
// (elapsed slots stay visible for two hours) and a Pacific one likewise loses
// an hour of bookable slots. Grouping by tz keeps the SQL to one CASE arm per
// distinct zone — seven, not fifty.
const STATE_TZ = {
  CT: "America/New_York", DE: "America/New_York", FL: "America/New_York",
  GA: "America/New_York", IN: "America/New_York", KY: "America/New_York",
  ME: "America/New_York", MD: "America/New_York", MA: "America/New_York",
  MI: "America/New_York", NH: "America/New_York", NJ: "America/New_York",
  NY: "America/New_York", NC: "America/New_York", OH: "America/New_York",
  PA: "America/New_York", RI: "America/New_York", SC: "America/New_York",
  VT: "America/New_York", VA: "America/New_York", WV: "America/New_York",
  DC: "America/New_York",
  AL: "America/Chicago", AR: "America/Chicago", IL: "America/Chicago",
  IA: "America/Chicago", KS: "America/Chicago", LA: "America/Chicago",
  MN: "America/Chicago", MS: "America/Chicago", MO: "America/Chicago",
  NE: "America/Chicago", ND: "America/Chicago", OK: "America/Chicago",
  SD: "America/Chicago", TN: "America/Chicago", TX: "America/Chicago",
  WI: "America/Chicago",
  CO: "America/Denver", MT: "America/Denver", NM: "America/Denver",
  UT: "America/Denver", WY: "America/Denver", ID: "America/Denver",
  AZ: "America/Phoenix",                       // no DST
  CA: "America/Los_Angeles", NV: "America/Los_Angeles",
  OR: "America/Los_Angeles", WA: "America/Los_Angeles",
  AK: "America/Anchorage", HI: "Pacific/Honolulu",
};

// Florida straddles two timezones: the panhandle west of the Apalachicola
// River is Central. Judging those rows by New_York hid (and cron-pruned)
// their next hour of bookable slots all day. City-level carve-out because
// rows carry city but not county; mirror any edit in scraper/d1.py
// FL_CENTRAL_CITIES. (Port St. Joe, Carrabelle and Tallahassee are Eastern.)
const FL_CENTRAL_CITIES = [
  "Bonifay", "Crestview", "DeFuniak Springs", "Destin", "Fort Walton Beach", "Freeport",
  "Gulf Breeze", "Hurlburt Field", "Lynn Haven", "Milton", "Miramar Beach",
  "Navarre", "Niceville", "Pace", "Panama City", "Panama City Beach",
  "Pensacola", "Shalimar", "Sunny Hills", "Watersound",
];
const FL_CENTRAL_SQL = FL_CENTRAL_CITIES.map((c) => `'${c}'`).join(",");
const FL_CENTRAL_ARM = `state = 'FL' AND COALESCE(city,'') IN (${FL_CENTRAL_SQL})`;

// Rows whose state is null/blank are judged by the LAST US zone to reach a
// given clock time. Conservative on purpose: it can leave a stale slot up a few
// extra hours, but it will never hide one that is still bookable.
const FALLBACK_TZ = "Pacific/Honolulu";

const tzGroups = () => {
  const g = {};
  for (const [st, tz] of Object.entries(STATE_TZ)) (g[tz] ||= []).push(st);
  return g;
};

// `teetime >= <local now for that row's state>`, as a CASE over tz groups.
//
// The state lists are inlined as SQL literals rather than bound. They come from
// the constant above — never from a request — and binding all 51 would eat 59
// of D1's 100-parameter-per-query ceiling, leaving almost nothing for the
// actual filters. Inlining keeps it at 9 binds: one clock per zone,
// plus the FL-panhandle arm's Chicago clock.
const TZ_ORDER = Object.entries(tzGroups());
// The FL-panhandle arm comes FIRST so it wins over FL's Eastern group arm.
const PAST_CLAUSE = `teetime >= CASE WHEN ${FL_CENTRAL_ARM} THEN ? ${TZ_ORDER
  .map(([, states]) => `WHEN state IN (${states.map((s) => `'${s}'`).join(",")}) THEN ?`)
  .join(" ")} ELSE ? END`;
const pastFilter = () => ({
  clause: PAST_CLAUSE,
  binds: [localNowISO("America/Chicago"),
          ...TZ_ORDER.map(([tz]) => localNowISO(tz)), localNowISO(FALLBACK_TZ)],
});

// ?include_past=1 disables the past filter; "0"/"false"/"no" must NOT — any
// non-empty value used to count as "include", so include_past=0 meant "yes".
const wantsPast = (p) => {
  const v = (p.get("include_past") || "").toLowerCase();
  return v !== "" && v !== "0" && v !== "false" && v !== "no";
};

// Merge facility name + sub-course label into one display name. If the label
// shares a significant word with the facility name it stands alone ("Hyland
// Hills Gold Course"); otherwise append it ("Legacy Ridge … · LR Back 9").
const displayName = (name, label) => {
  if (!label) return name;
  const words = new Set(
    (name || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 3));
  const shares = (label.toLowerCase().split(/[^a-z0-9]+/) || [])
    .some((w) => words.has(w));
  return shares ? label : `${name} · ${label}`;
};

// --- Freshness guard -------------------------------------------------------
// Show a slot only if we RECENTLY re-confirmed its course's sheet for its date
// (sheet_freshness.last_ok_at, stamped by the scraper on every clean scrape).
// When a scraper stalls — CPS behind Cloudflare, kenna throttling — its rows
// stay in the table (active, shielded from wrongful deletion) but drop OFF the
// live site until a scrape re-confirms them, so a booked slot can't sit as a
// phantom for hours. This is the systemic fix for "the site showed a tee time
// that wasn't really available".
//
// Grace is date-aware, matched to how often each horizon is ACTUALLY
// re-confirmed (grace must be >= the slowest healthy re-scrape interval for the
// tier, or the guard hides real slots):
//   day 0     (today): 3 h   (see the 2026-08-04 part-2 note below)
//   days 1-2  (near tier): 6 h
//   days 3-7  (mid tier): 18 h
//   days 8-30 (far tier, residential browser refreshes DAILY): 30 h
// Missing freshness row = never scraped since this shipped = shown (cold-start
// safe; we only hide on PROVEN staleness, never on absence of data).
//
// 2026-08-04: mid raised 6h -> 18h. The 6h assumed "hourly plain" re-confirms,
// but the mid tier's real re-scrape interval for a given (course,date) is much
// longer: teeitup rides its OWN browser job over days 3-30 SHUFFLED (28 dates),
// so any one date is re-confirmed only every ~5-6 h on average and longer in a
// bad stretch; cps/ezlinks/etc. browser jobs cover days 0-6 but take ~15 min a
// date. So a (course,date) legitimately scraped, with unchanged 3-7-day-out
// availability, routinely aged past 6 h and got HIDDEN — measured as scattered
// per-date holes across CO days 3-7 (twin-peaks day6, ute-creek days3-4,
// olde-loveland days6-7, riverdale-dunes days3-4, ...). Per this guard's own
// rule (grace >= slowest healthy re-scrape interval) 6 h was simply too low.
// 18 h covers the real cadence while still hiding a scraper that has been dead
// most of a day; 3-7-day-out availability is stable enough that 18-h-old data
// is safe to show.
// 2026-08-04 (part 2): the near tier was ALSO too tight. The same browser
// scrapers own days 0-2 (cps now ~39 tenants + Home/Configuration fetch,
// teeitup its own near loop, ezlinks/clubcaddie/golfnow/...): one self-chaining
// pass over days 0-2 takes well over the old 90-min grace, so days 1-2 for the
// browser platforms routinely aged past 90 min and got HIDDEN — measured on CO:
// ~25 courses (cattail-creek/flatirons/indian-peaks/indian-tree cps,
// antler-creek/colorado-national/buffalo-run teeitup, arrowhead golfnow, ...)
// showed on day 3 (18 h tier) but were blank on days 1-2. Split the near tier:
//   day 0    (today): 3 h  — still tight (phantom-slot risk highest today, and
//                            past-time pruning independently drops elapsed slots)
//   days 1-2 (near):  6 h  — covers the real browser near-pass cadence
// (day 0 is scraped FIRST each unshuffled pass so it's the freshest of the three,
//  which is why 3 h is enough there while days 1-2 need 6 h.)
const FRESH_TODAY_DAYS = 0, FRESH_TODAY_MIN = 3 * 60;   // day 0 (today)
const FRESH_NEAR_DAYS = 2,  FRESH_NEAR_MIN = 6 * 60;     // days 1-2
const FRESH_MID_DAYS = 7,   FRESH_MID_MIN = 18 * 60;     // days 3-7
const FRESH_FAR_MIN = 30 * 60;                            // days 8-30
const utc19 = (ms) => new Date(ms).toISOString().slice(0, 19);
function freshnessFilter() {
  const now = Date.now();
  const denverToday = new Date()
    .toLocaleString("sv-SE", { timeZone: "America/Denver" }).slice(0, 10);
  const boundary = (days) => {
    const b = new Date(denverToday + "T00:00:00Z");
    b.setUTCDate(b.getUTCDate() + days);
    return b.toISOString().slice(0, 10);
  };
  const todayBoundary = boundary(FRESH_TODAY_DAYS);   // today (day 0)
  const nearBoundary = boundary(FRESH_NEAR_DAYS);     // today+2 (days 1-2)
  const midBoundary = boundary(FRESH_MID_DAYS);       // today+7 (days 3-7)
  const todayCut = utc19(now - FRESH_TODAY_MIN * 60000);
  const nearCut = utc19(now - FRESH_NEAR_MIN * 60000);
  const midCut = utc19(now - FRESH_MID_MIN * 60000);
  const farCut = utc19(now - FRESH_FAR_MIN * 60000);
  // Hide the row iff a freshness record exists AND is stale for its date-tier.
  // CASE WHENs are evaluated in order, so day 0 is caught before days 1-2, etc.
  const clause =
    "NOT EXISTS (SELECT 1 FROM sheet_freshness sf " +
    "WHERE sf.course_slug = tee_times.course_slug " +
    "AND sf.date = substr(tee_times.teetime,1,10) " +
    "AND substr(sf.last_ok_at,1,19) < CASE " +
    "WHEN substr(tee_times.teetime,1,10) <= ? THEN ? " +
    "WHEN substr(tee_times.teetime,1,10) <= ? THEN ? " +
    "WHEN substr(tee_times.teetime,1,10) <= ? THEN ? ELSE ? END)";
  return { clause, binds: [todayBoundary, todayCut, nearBoundary, nearCut,
                           midBoundary, midCut, farCut] };
}
let freshnessReady = false;
async function ensureFreshnessTable(env) {
  if (freshnessReady) return;
  await env.DB.prepare(
    "CREATE TABLE IF NOT EXISTS sheet_freshness (course_slug TEXT NOT NULL, " +
    "date TEXT NOT NULL, last_ok_at TEXT NOT NULL, " +
    "PRIMARY KEY (course_slug, date))").run();
  freshnessReady = true;
}

// --- Live click-time re-validation -----------------------------------------
// At click time the frontend asks: is THIS slot still bookable at the source
// RIGHT NOW? We re-hit the booking source live and answer open | gone | unknown.
//
// SAFETY CONTRACT: return "gone" ONLY when we positively confirm the slot is
// absent from a successfully-fetched sheet. Any error, timeout, missing ids, or
// not-yet-implemented platform => "unknown". The frontend treats anything other
// than "gone" as "proceed to booking" (today's behavior), so re-validation can
// NEVER block a valid booking and can be rolled out one platform at a time.
//
// Only the PLAIN (datacenter-reachable) platforms are checkable from a Worker.
// The Cloudflare-challenged / browser platforms (teeitup, cps-challenged incl.
// Indian Tree, ezlinks, golfnow, clubcaddie, ...) are 403 from a datacenter IP,
// so they are not in REVALIDATE_IDS and always answer "unknown" — they rely on
// the scrape freshness guard instead.
const REVAL_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const REVAL_TIMEOUT_MS = 4500;   // keep the click snappy; slow source => unknown

// Each revalidator returns a Set of bookable "YYYY-MM-DD HH:MM" local strings
// for `date`, or null on ANY failure (=> unknown). `ids` is the registry handle.
const REVALIDATORS = {
  // TenFore: one open GET to /api/TeeSheet (no auth, no reCAPTCHA). Bookable =
  // availableSlots>0 AND not an all-"Block" slot (matches scraper/adapters/tenfore.py).
  async tenfore(ids, date, signal) {
    const gid = ids.golf_course_id;
    if (!gid) return null;
    const u = `https://swan.tenfore.golf/api/TeeSheet?golfCourseId=${encodeURIComponent(gid)}` +
              `&startDate=${date}&endDate=${date}`;
    const r = await fetch(u, { signal, headers: {
      "x-tenfore-appid": "23", "Origin": "https://fox.tenfore.golf",
      "Referer": "https://fox.tenfore.golf/", "Accept": "application/json",
      "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const j = await r.json();
    const set = new Set();
    for (const t of ((j && j.teeTimes) || [])) {
      const iso = String(t.dateScheduled || "").slice(0, 16); // YYYY-MM-DDTHH:MM
      if (iso.slice(0, 10) !== date) continue;
      if (!(typeof t.availableSlots === "number" && t.availableSlots > 0)) continue;
      const cs = t.teeTimeCustomers || [];
      if (cs.length && cs.every((c) => (c.teeTimeCustomerTypeName || "").includes("Block"))) continue;
      set.add(iso.replace("T", " "));
    }
    return set;
  },
  // foreUP: one GET to the anonymous booking/times API (api_key=no_limits).
  async foreup(ids, date, signal) {
    const cid = ids.course_id;
    if (!cid) return null;
    const [Y, M, D] = date.split("-");
    let u = `https://foreupsoftware.com/index.php/api/booking/times?time=all` +
            `&date=${M}-${D}-${Y}&holes=all&players=0&api_key=no_limits` +
            `&course_id=${encodeURIComponent(cid)}`;
    if (ids.schedule_id) u += `&schedule_id=${encodeURIComponent(ids.schedule_id)}`;
    const r = await fetch(u, { signal, headers: {
      "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
      "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const j = await r.json();
    if (!Array.isArray(j)) return null;
    const set = new Set();
    for (const t of j) {
      if (!(t && typeof t.available_spots === "number" && t.available_spots > 0)) continue;
      const tm = String(t.time || "").slice(0, 16); // "YYYY-MM-DD HH:MM"
      if (tm.slice(0, 10) === date) set.add(tm);
    }
    return set;
  },
  // Chronogolf (Lightspeed): 3-call chain — club -> affiliation + course_ids,
  // then marketplace teetimes per course. Discovery (club_id/aff/course_ids) is
  // stable, so cache it per-isolate. Union bookable times across sub-courses:
  // "open if the slot is bookable on ANY of the club's courses" is conservative
  // (a false "open" just proceeds; we never fabricate a "gone"). Mirrors
  // scraper/adapters/chronogolf.py (nb_holes=18, out_of_capacity = full).
  async chronogolf(ids, date, signal) {
    const B = "https://www.chronogolf.com";
    const key = ids.club_id || ids.slug;
    if (!key) return null;
    let disc = CG_DISC.get(String(key));
    if (!disc) {
      const cr = await fetch(`${B}/private_api/clubs/${encodeURIComponent(key)}`,
        { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
      if (!cr.ok) return null;
      const club = await cr.json();
      const clubId = club && club.id;
      const aff = club && club.settings && club.settings.default_affiliation_type_id;
      // unclaimed directory listing (online_booking_enabled=false) or no public
      // affiliation => not bookable through Chronogolf; treat as unknown.
      if (!clubId || !aff || club.online_booking_enabled === false) return null;
      let courseIds = Array.isArray(ids.course_ids) && ids.course_ids.length ? ids.course_ids : null;
      if (!courseIds) {
        const csr = await fetch(`${B}/private_api/clubs/${clubId}/courses`,
          { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
        if (!csr.ok) return null;
        const cj = await csr.json();
        const arr = Array.isArray(cj) ? cj : (cj.courses || []);
        courseIds = arr.filter((c) => c.online_booking_enabled).map((c) => c.id);
      }
      if (!courseIds || !courseIds.length) return null;
      disc = { clubId, aff, courseIds };
      CG_DISC.set(String(key), disc);
    }
    const results = await Promise.all(disc.courseIds.map(async (cid) => {
      const u = `${B}/marketplace/clubs/${disc.clubId}/teetimes?date=${date}` +
                `&course_id=${encodeURIComponent(cid)}` +
                `&affiliation_type_ids[]=${encodeURIComponent(disc.aff)}&nb_holes=18`;
      try {
        const r = await fetch(u, { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
        if (!r.ok) return null;
        const j = await r.json();
        return Array.isArray(j) ? j : null;
      } catch (e) { return null; }
    }));
    let anyOk = false;
    const set = new Set();
    for (const slots of results) {
      if (!slots) continue;
      anyOk = true;
      for (const s of slots) {
        if (s.out_of_capacity) continue;
        const st = String(s.start_time || "").slice(0, 5); // "HH:MM"
        if (st) set.add(`${s.date || date} ${st}`);
      }
    }
    return anyOk ? set : null;   // all teetimes calls failed => unknown (safe)
  },
  // GolfBack: one anonymous POST. localDateTime is the club wall-clock (the
  // dateTime field is UTC-labelled and must NOT be used). isAvailable=false =>
  // not bookable; courseId must match the uuid we asked for.
  async golfback(ids, date, signal) {
    const uuid = ids.course_uuid;
    if (!uuid) return null;
    const r = await fetch(
      `https://api.golfback.com/api/v1/courses/${uuid}/date/${date}/teetimes`,
      { method: "POST", signal, body: JSON.stringify({ sessionId: null }),
        headers: { "Content-Type": "application/json", "Accept": "application/json",
          "Origin": "https://golfback.com", "Referer": "https://golfback.com/",
          "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const j = await r.json();
    const rows = (j && j.data) || [];
    if (!Array.isArray(rows)) return null;
    const set = new Set();
    for (const row of rows) {
      if (!row || row.isAvailable === false) continue;
      if (row.courseId && row.courseId !== uuid) continue;
      const local = String(row.localDateTime || "");
      if (local.slice(0, 10) === date) set.add(local.slice(0, 10) + " " + local.slice(11, 16));
    }
    return set;
  },
  // MemberSports: POST per configurationTypeId (sweep config_ids). A slot is
  // bookable if any item is not booking-blocked/hidden and has an open seat.
  async membersports(ids, date, signal) {
    const clubId = ids.club_id ? parseInt(ids.club_id, 10) : null;
    if (!clubId) return null;
    const cfgs = (Array.isArray(ids.config_ids) && ids.config_ids.length)
      ? ids.config_ids.map(Number) : [0];
    const want = (Array.isArray(ids.course_ids) && ids.course_ids.length)
      ? new Set(ids.course_ids.map(Number)) : null;
    const set = new Set();
    let anyOk = false;
    for (const cfg of cfgs) {
      let j;
      try {
        const r = await fetch("https://api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes",
          { method: "POST", signal, headers: {
              "Content-Type": "application/json", "Accept": "application/json",
              "x-api-key": "A9814038-9E19-4683-B171-5A06B39147FC",
              "Origin": "https://app.membersports.com",
              "Referer": "https://app.membersports.com/", "User-Agent": REVAL_UA },
            body: JSON.stringify({ configurationTypeId: cfg, date: date,
              golfClubGroupId: 0, golfClubId: clubId, golfCourseId: 0, groupSheetTypeId: 0 }) });
        if (!r.ok) continue;
        j = await r.json();
      } catch (e) { continue; }
      if (!Array.isArray(j)) continue;
      anyOk = true;
      for (const row of j) {
        const tm = row && row.teeTime;
        if (tm == null) continue;
        const ok = (row.items || []).some((it) => {
          if (!it) return false;
          if (want && !want.has(Number(it.golfCourseId))) return false;
          if (it.bookingNotAllowed || it.hide) return false;
          return (4 - (Number(it.playerCount) || 0)) > 0;
        });
        if (!ok) continue;
        const hh = Math.floor(tm / 60), mm = tm % 60;
        set.add(`${date} ${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`);
      }
    }
    return anyOk ? set : null;
  },
  // CourseCo (Total-e Integrated): one GET to the per-tenant gateway. Origin
  // must match the tenant host (hazard 1). Time is "HH:MM:SS:mmm".
  async courseco(ids, date, signal) {
    const tenant = ids.tenant, gateway = ids.gateway, cid = ids.course_id;
    if (!tenant || !gateway || !cid) return null;
    const host = `https://${tenant}.totaleintegrated.net`;
    const qs = new URLSearchParams({
      IsInitTeeTimeRequest: "false", TeeTimeDate: date, CourseID: cid,
      StartTime: "05:00", EndTime: "21:00", NumOfPlayers: "0", Holes: "-1",
      IsNineHole: "-1", StartPrice: "0", EndPrice: "", CartIncluded: "false",
      SpecialsOnly: "0", IsClosest: "0", PlayerIDs: "", DateFilterChange: "false",
      DateFilterChangeNoSearch: "false", SearchByGroups: "true", IsPrepaidOnly: "0",
      CourseFavoritesChecked: "true", QueryStringFilters: "null" });
    const r = await fetch(
      `https://${gateway}-gateway.totaleintegrated.net/Booking/Teetimes?${qs}`,
      { signal, headers: { "Origin": host, "Referer": host + "/",
          "Accept": "application/json, text/plain, */*", "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const j = await r.json();
    const rows = (j && j.TeeTimeData) || [];
    if (!Array.isArray(rows)) return null;
    const set = new Set();
    for (const s of rows) {
      if (s.CourseID && String(s.CourseID) !== String(cid)) continue;
      let day = date;
      const dm = String(s.TTDate || "").match(/^(\d{2})\/(\d{2})\/(\d{4})/);
      if (dm) day = `${dm[3]}-${dm[1]}-${dm[2]}`;
      const parts = String(s.Time || "").split(":");
      if (parts.length >= 2) {
        set.add(`${day} ${parts[0].padStart(2, "0")}:${parts[1].padStart(2, "0")}`);
      }
    }
    return set;
  },
  // rGuest (Agilysys): mint anonymous token -> (courses if not pinned) ->
  // getAvailableTeeSlots per course. 501 = empty sheet (a valid "no rows").
  async rguest(ids, date, signal) {
    const tenant = ids.tenant, prop = ids.property;
    if (!tenant || !prop) return null;
    const B = "https://book.rguest.com";
    const G = `${B}/wbe-golf-service/golf/tenants/${tenant}/propertyId/${prop}`;
    const tz = ids.timezone || "America/Phoenix";
    const tkey = tenant + "/" + prop;
    let tok = RG_TOK.get(tkey);
    if (!tok || tok.exp < Date.now()) {
      const tr = await fetch(
        `${B}/wbe-admin-service/generatetoken/v2/tenants/${tenant}/propertyId/${prop}/appName/NA`,
        { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
      if (!tr.ok) return null;
      const tj = await tr.json();
      if (!tj || !tj.token) return null;
      tok = { token: tj.token, exp: Date.now() + 40 * 60 * 1000 };
      RG_TOK.set(tkey, tok);
    }
    const headers = { "Authorization": "Bearer " + tok.token, "timeZone": tz,
      "propertyDTTM": `${date}T00:00:00`, "Accept": "application/json, text/plain, */*",
      "User-Agent": REVAL_UA };
    let courseIds = ids.course_id != null ? [Number(ids.course_id)] : null;
    if (!courseIds) {
      const cr = await fetch(`${G}/getAvailableCourses?appName=golf`, { signal, headers });
      if (!cr.ok) return null;
      const cj = await cr.json();
      courseIds = ((cj && cj.availableCourses) || []).map((c) => c.id).filter((x) => x != null);
    }
    if (!courseIds.length) return null;
    const set = new Set();
    let anyOk = false;
    const results = await Promise.all(courseIds.map(async (cid) => {
      const p = new URLSearchParams({ fromDate: date, toDate: date, courseId: String(cid),
        playerTypeId: "0", holes: "0", appName: "golf", dateTime: `${date}T00:00:00` });
      try {
        const r = await fetch(`${G}/getAvailableTeeSlots?${p}`, { signal, headers });
        if (r.status === 501) return [];          // empty sheet, a valid answer
        if (!r.ok) return null;
        const j = await r.json();
        return (j && j.availableTeeSlots) || [];
      } catch (e) { return null; }
    }));
    for (const groups of results) {
      if (groups === null) continue;
      anyOk = true;
      for (const g of groups) {
        for (const s of (g.slots || [])) {
          if (!(typeof s.availability === "number" && s.availability > 0)) continue;
          const t = String(s.scheduleDateTime || "");
          if (t.slice(0, 10) === date) set.add(t.slice(0, 10) + " " + t.slice(11, 16));
        }
      }
    }
    return anyOk ? set : null;
  },
  // Teesnap: homepage window.courses (bracket-matched) unless ids pinned, then
  // teetimes-day per course. ?course= resolves GLOBALLY, so only pinned or
  // this-tenant-owned ids are used (see scraper/adapters/teesnap.py).
  async teesnap(ids, date, signal) {
    const sub = ids.subdomain;
    if (!sub) return null;
    const B = `https://${sub}.teesnap.net`;
    let courseIds = (Array.isArray(ids.teesnap_course_ids) && ids.teesnap_course_ids.length)
      ? ids.teesnap_course_ids.map(Number) : null;
    if (!courseIds) {
      courseIds = TS_DISC.get(sub) || null;
      if (!courseIds) {
        const hr = await fetch(B + "/", { signal, headers: { "Accept": "text/html", "User-Agent": REVAL_UA } });
        if (!hr.ok) return null;
        courseIds = tsParseCourses(await hr.text());
        if (courseIds.length) TS_DISC.set(sub, courseIds);
      }
    }
    if (!courseIds || !courseIds.length) return null;
    const set = new Set();
    let anyOk = false;
    const results = await Promise.all(courseIds.map(async (cid) => {
      const p = new URLSearchParams({ course: String(cid), date: date, players: "1", holes: "18", addons: "off" });
      try {
        const r = await fetch(`${B}/customer-api/teetimes-day?${p}`,
          { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
        if (!r.ok) return null;
        const j = await r.json();
        return ((j && j.teeTimes) || {}).teeTimes || [];
      } catch (e) { return null; }
    }));
    for (const slots of results) {
      if (slots === null) continue;
      anyOk = true;
      for (const slot of slots) {
        const times = [];
        for (const sec of (slot.teeOffSections || [])) {
          const t = (sec.turnTo && sec.turnTo.time) || sec.time;
          if (t) times.push(t);
        }
        if (!times.length && slot.teeTime) times.push(slot.teeTime);
        for (const t of times) {
          const iso = String(t).slice(0, 16);   // "YYYY-MM-DDTHH:MM"
          if (iso.slice(0, 10) === date) set.add(iso.replace("T", " "));
        }
      }
    }
    return anyOk ? set : null;
  },
  // ForeTees public portal: plain JSON. openSlots>0 = bookable.
  async foretees(ids, date, signal) {
    const key = ids.club_key, cid = ids.cid;
    if (!key || !cid) return null;
    const u = `https://web.foretees.com/v5/servlet/Public_teesheet` +
              `?cid=${encodeURIComponent(cid)}&ckey=${encodeURIComponent(key)}&a=vts&d=${date}`;
    const r = await fetch(u, { signal, headers: { "Accept": "application/json", "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const j = await r.json();
    const blocks = ((j.foreTeesPublicTimesApiResp || {}).data) || [];
    const set = new Set();
    for (const b of blocks) {
      for (const s of (b.publicTimes || [])) {
        if (!(typeof s.openSlots === "number" && s.openSlots > 0)) continue;
        const d = s.date || date;
        const t = String(s.time || "").slice(0, 5);   // "HH:MM"
        if (d === date && t) set.add(`${d} ${t}`);
      }
    }
    return set;
  },
  // Quick18 (SagaCity): server-rendered searchmatrix table. No DOM in Workers,
  // so pull time-cells by regex. GUARD: require the "searchmatrix" marker so a
  // maintenance/error page can never read as an empty sheet (=> false "gone").
  // Only a time is needed (the sheet lists bookable slots); false-open is safe.
  async quick18(ids, date, signal) {
    const sub = ids.subdomain;
    if (!sub) return null;
    const domain = ids.domain || "quick18.com";
    const u = `https://${sub}.${domain}/teetimes/searchmatrix?teedate=${date.replace(/-/g, "")}`;
    const r = await fetch(u, { signal, headers: { "Accept": "text/html", "User-Agent": REVAL_UA } });
    if (!r.ok) return null;
    const html = await r.text();
    if (!/searchmatrix/i.test(html)) return null;   // not the sheet => unknown
    const set = new Set();
    const re = /<td[^>]*>\s*(\d{1,2}):(\d{2})\s*([AP])M\b/gi;
    let m;
    while ((m = re.exec(html))) {
      let h = parseInt(m[1], 10) % 12;
      if (m[3].toUpperCase() === "P") h += 12;
      set.add(`${date} ${String(h).padStart(2, "0")}:${m[2]}`);
    }
    // Empty parse => we did NOT read a real sheet (these hosts serve the Worker
    // a JS-shell/challenge page that still contains "searchmatrix"). Treat as
    // unknown, never "gone". A real booked-slot catch still works: a populated
    // sheet missing one time yields a non-empty set.
    return set.size ? set : null;
  },
  // TeeQuest: two skins. v2 has structured attributes; legacy is a form POST.
  async teequest(ids, date, signal) {
    const site = ids.site;
    if (!site) return null;
    const skin = ids.skin || (ids.host === "bookateetime.teequest.com" ? "v2" : "legacy");
    const ymd = date.replace(/-/g, "");
    if (skin === "v2") {
      const tags = (Array.isArray(ids.course_tags) && ids.course_tags.length)
        ? ids.course_tags : [`${site}-1`];
      const set = new Set();
      let anyOk = false;
      for (const tag of tags) {
        for (const players of [1, 2]) {
          let html;
          try {
            const r = await fetch(
              `https://bookateetime.teequest.com/search/${tag}/${date}` +
              `?selectedPlayers=${players}&selectedHoles=18`,
              { signal, headers: { "Accept": "text/html", "User-Agent": REVAL_UA } });
            if (!r.ok) break;
            html = await r.text();
          } catch (e) { break; }
          anyOk = true;
          if (players === 1 && /does not allow single player/i.test(html)) continue;
          const tagRe = /<[^>]*\bdata-date-time="(\d{12})"[^>]*>/gi;
          let tm;
          while ((tm = tagRe.exec(html))) {
            const raw = tm[1];
            if (raw.slice(0, 8) !== ymd) continue;
            const av = (tm[0].match(/data-available="(\d+)"/i) || [])[1];
            if (av !== undefined && parseInt(av, 10) <= 0) continue;
            set.add(`${date} ${raw.slice(8, 10)}:${raw.slice(10, 12)}`);
          }
          break;   // handled this tag
        }
      }
      return set.size ? set : null;   // empty parse => unknown, never false-gone
    }
    // legacy: GET shell for the offered course tags, POST search per tag,
    // collect .time-container times. false-open is safe, so we don't parse the
    // per-slot player links — presence on the sheet is enough for "not gone".
    const home = `https://teetimes.teequest.com/${site}`;
    let shell;
    try {
      const r = await fetch(home, { signal, headers: { "Accept": "text/html", "User-Agent": REVAL_UA } });
      if (!r.ok) return null;
      shell = await r.text();
    } catch (e) { return null; }
    const offered = [];
    const selM = shell.match(/<select[^>]*name="Search\.CourseTag"[^>]*>([\s\S]*?)<\/select>/i);
    if (selM) {
      const optRe = /<option[^>]*value="([^"]+)"/gi;
      let om;
      while ((om = optRe.exec(selM[1]))) offered.push(om[1]);
    }
    let tags = (Array.isArray(ids.course_tags) && ids.course_tags.length)
      ? ids.course_tags : offered;
    if (offered.length) tags = tags.filter((t) => offered.includes(t));
    if (!tags.length) return null;
    const [Y, M, D] = date.split("-");
    const set = new Set();
    let anyOk = false;
    for (const tag of tags) {
      const body = new URLSearchParams({
        "PaymentTab": "pay-online", "Search.CourseTag": tag,
        "Search.Date": `${parseInt(M, 10)}/${parseInt(D, 10)}/${Y} 12:00:00 AM`,
        "Search.Time": "Anytime", "Search.Players": "0" });
      let html;
      try {
        const r = await fetch(home, { method: "POST", signal, body: body.toString(),
          headers: { "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html", "User-Agent": REVAL_UA } });
        if (!r.ok) continue;
        html = await r.text();
      } catch (e) { continue; }
      anyOk = true;
      const re = /class="[^"]*time-container[^"]*"[^>]*>\s*(\d{1,2}):(\d{2})\s*([ap])m/gi;
      let m;
      while ((m = re.exec(html))) {
        let h = parseInt(m[1], 10) % 12;
        if (m[3].toLowerCase() === "p") h += 12;
        set.add(`${date} ${String(h).padStart(2, "0")}:${m[2]}`);
      }
    }
    return set.size ? set : null;   // empty parse => unknown, never false-gone
  },
};
// Per-isolate caches (best-effort; isolates are short-lived).
const CG_DISC = new Map();   // chronogolf: slug/club_id -> {clubId, aff, courseIds}
const RG_TOK = new Map();    // rguest: tenant/prop -> {token, exp}
const TS_DISC = new Map();   // teesnap: subdomain -> [course ids]
let GEO_CACHE = null;        // venue_geo: venue_id -> [lat, lng] (whole table)

// One read of venue_geo per isolate, reused for every /api/directory response.
// The table is tiny (~1 row per venue) and changes only when the geocode
// backfill runs, so caching it beats a JOIN-per-request. Missing table / any
// error -> empty map, and every course just reports lat/lng null (the near-me
// filter already tolerates that for un-geocoded venues).
async function venueGeo(env) {
  if (GEO_CACHE) return GEO_CACHE;
  const m = new Map();
  try {
    const { results } = await env.DB.prepare(
      "SELECT venue_id, lat, lng FROM venue_geo").all();
    for (const r of results) {
      if (r.lat != null && r.lng != null) m.set(r.venue_id, [r.lat, r.lng]);
    }
  } catch (e) { /* table absent / read error -> no geo, courses get null */ }
  GEO_CACHE = m;
  return m;
}

// Teesnap: the TOP-LEVEL ids of `window.courses = [...]` (bracket-matched +
// JSON-parsed, never regexed — a course object embeds its parent property's own
// id). Enabled, non-deleted only. [] on any parse failure.
function tsParseCourses(html) {
  const m = html.match(/window\.courses\s*=\s*/);
  if (!m) return [];
  const start = html.indexOf("[", m.index + m[0].length);
  if (start < 0) return [];
  let depth = 0, i = start, inStr = false, esc = false;
  for (; i < html.length; i++) {
    const ch = html[i];
    if (inStr) { if (esc) esc = false; else if (ch === "\\") esc = true; else if (ch === '"') inStr = false; }
    else if (ch === '"') inStr = true;
    else if (ch === "[" || ch === "{") depth++;
    else if (ch === "]" || ch === "}") { depth--; if (depth === 0) break; }
  }
  let arr;
  try { arr = JSON.parse(html.slice(start, i + 1)); } catch (e) { return []; }
  if (!Array.isArray(arr)) return [];
  return arr.filter((c) => c && typeof c.id === "number" && !c.deleted_at &&
                    c.enabled !== false && (c.key != null || c.name != null))
            .map((c) => c.id);
}

async function handleRevalidate(url) {
  const p = url.searchParams;
  // ?list=1 -> the set of venue_ids that ARE revalidatable, so the frontend
  // only intercepts those clicks and lets every other booking link open
  // instantly (no check, no delay). Cached long at the edge; changes on deploy.
  if (p.get("list")) {
    return new Response(
      JSON.stringify({ venues: Object.keys(REVALIDATE_IDS) }),
      { headers: { "Content-Type": "application/json",
                   "Cache-Control": "public, max-age=1800, s-maxage=3600", ...CORS } });
  }
  const course = p.get("course");          // venue_id (what /api hands out)
  const teetime = p.get("teetime");        // "YYYY-MM-DDTHH:MM:SS" (course-local)
  if (!course || !teetime || teetime.length < 16) {
    return json({ status: "unknown", reason: "missing course/teetime" });
  }
  const handle = REVALIDATE_IDS[course];
  if (!handle) return json({ status: "unknown", reason: "not revalidatable" });
  const rev = REVALIDATORS[handle.p];
  if (!rev) return json({ status: "unknown", reason: `no revalidator: ${handle.p}` });
  const date = teetime.slice(0, 10);
  const wantHM = teetime.slice(0, 16).replace("T", " ");   // "YYYY-MM-DD HH:MM"
  let set = null;
  try {
    set = await rev(handle.i, date, AbortSignal.timeout(REVAL_TIMEOUT_MS));
  } catch (e) { /* timeout / network / parse => unknown */ }
  if (!set) return json({ status: "unknown", reason: "source unavailable", platform: handle.p });
  // Only a POSITIVE miss on a good sheet is "gone".
  return json({ status: set.has(wantHM) ? "open" : "gone",
                platform: handle.p, checked: wantHM });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);

    const { clause: pastClause, binds: pastBinds } = pastFilter();
    // Guarantee the freshness table exists before any read references it
    // (once per isolate). Removes any deploy-ordering dependency on the
    // scraper having created it first.
    try { await ensureFreshnessTable(env); } catch (e) { /* non-fatal */ }

    try {
      if (url.pathname === "/api/revalidate") {
        return handleRevalidate(url);
      }

      if (url.pathname === "/api/health") {
        // Liveness + last-scrape stats WITHOUT scanning tee_times. The old
        // "SELECT COUNT(*), SUM(active) FROM tee_times" read the ENTIRE table
        // (~1.38M rows) on every hit, uncached — a monitor polling this endpoint
        // was the bulk of D1's row-read bill (519B reads). Read one indexed row
        // from `runs` (PK desc) instead — zero table scan — and let the edge
        // cache it so repeated polls don't even reach D1.
        let r = null;
        try {
          r = await env.DB.prepare(
            "SELECT generated_at, date, tee_times, courses_ok, courses_queried " +
            "FROM runs ORDER BY id DESC LIMIT 1").first();
        } catch (e) { /* runs empty on a fresh DB — still report liveness */ }
        return json({ ok: true, last_run: r || null }, 200,
                    { "Cache-Control": "public, max-age=60" });
      }

      // Every course we know of, bookable by us or not — the answer to "is
      // this course missing, or just not bookable online?", which the golfer
      // cannot tell apart from an empty result.
      //
      // Served from the bundle, not D1: it is derived from the state CSVs, so
      // it changes when a deploy happens and at no other time. A D1 table
      // would need a migration and a sync job to say exactly the same thing,
      // and would be the thing that silently drifts.
      //
      // Note what is NOT here: whether we currently have tee times. That is a
      // live fact the tee-time feed already answers; duplicating it here would
      // just be a badge that goes stale between deploys.
      if (url.pathname === "/api/directory") {
        const p = url.searchParams;
        const st = (p.get("state") || "").toUpperCase();
        const method = (p.get("method") || "").toLowerCase();
        const city = (p.get("city") || "").toLowerCase();
        const q = (p.get("q") || "").toLowerCase();
        let courses = DIRECTORY.courses;
        if (st) courses = courses.filter((c) => c.state === st);
        if (method) courses = courses.filter((c) => c.booking_method === method);
        if (city) courses = courses.filter((c) => (c.city || "").toLowerCase() === city);
        if (q) courses = courses.filter((c) => (c.name || "").toLowerCase().includes(q));
        // Attach lat/lng from venue_geo (same source the tee-times feed joins),
        // so a book-on-site directory entry carries coordinates too. Spread into
        // fresh objects — never mutate the shared DIRECTORY bundle.
        const geo = await venueGeo(env);
        courses = courses.map((c) => {
          const g = geo.get(c.venue_id);
          return { ...c, lat: g ? g[0] : null, lng: g ? g[1] : null };
        });
        return new Response(
          JSON.stringify({ count: courses.length, courses }),
          { headers: {
              "Content-Type": "application/json",
              // Static between deploys, so let the browser and the edge keep
              // it. Without this every widget load re-downloads 180KB that
              // did not change.
              "Cache-Control": "public, max-age=3600, s-maxage=86400",
              ...CORS } });
      }

      if (url.pathname === "/api/courses") {
        // One row per physical venue. Prefer the primary (native) source's
        // platform + booking link; count distinct upcoming times so a slot
        // listed by both the native engine and GolfNow isn't double-counted.
        const p = url.searchParams;
        const clauses = ["active = 1"];
        const binds = [];
        if (!wantsPast(p)) { clauses.push(pastClause); binds.push(...pastBinds); }
        if (p.get("state")) { clauses.push("state = ?");            binds.push(p.get("state").toUpperCase()); }
        if (p.get("city"))  { clauses.push("LOWER(city) = LOWER(?)"); binds.push(p.get("city")); }
        { const f = freshnessFilter(); clauses.push(f.clause); binds.push(...f.binds); }
        const { results } = await env.DB.prepare(
          `SELECT COALESCE(venue_id, course_slug) AS course_slug,
                  COALESCE(venue_id, course_slug) AS venue_id,
                  MAX(course_name) AS course_name,
                  MAX(city)        AS city,
                  MAX(state)       AS state,
                  COALESCE(MAX(CASE WHEN source_role = 'primary' THEN platform END),
                           MAX(platform))    AS platform,
                  COALESCE(MAX(CASE WHEN source_role = 'primary' THEN booking_url END),
                           MAX(booking_url)) AS booking_url,
                  COUNT(DISTINCT teetime || '|' || COALESCE(course_label,'')) AS slots,
                  MIN(price_min)   AS from_price
             FROM tee_times
            WHERE ${clauses.join(" AND ")}
            GROUP BY COALESCE(venue_id, course_slug)
            ORDER BY course_name`).bind(...binds).all();
        return json({ courses: results });
      }

      if (url.pathname === "/api/tee-times") {
        const p = url.searchParams;
        const clauses = ["active = 1"];
        const binds = [];
        if (!wantsPast(p)) { clauses.push(pastClause); binds.push(...pastBinds); }
        if (p.get("date"))      { clauses.push("substr(teetime,1,10) = ?"); binds.push(p.get("date")); }
        // state accepts one ("CO") or a comma list ("CO,WY,KS"): the site scopes
        // its per-platform query to the user's nearby states so a dense platform
        // (teeitup = 364 courses) can't overflow the row cap and drop afternoon
        // slots. Single-state stays an exact-equality (indexed) lookup.
        if (p.get("state")) {
          const _st = p.get("state").toUpperCase().split(",").map((s) => s.trim()).filter(Boolean);
          if (_st.length === 1) { clauses.push("state = ?"); binds.push(_st[0]); }
          else if (_st.length) { clauses.push("state IN (" + _st.map(() => "?").join(",") + ")"); binds.push(..._st); }
        }
        if (p.get("city"))      { clauses.push("LOWER(city) = LOWER(?)");   binds.push(p.get("city")); }
        // course filter is venue-aware: accepts a venue_id (what this API now
        // hands out as course_slug) or a legacy source slug.
        if (p.get("course"))    { clauses.push("COALESCE(venue_id, course_slug) = ?"); binds.push(p.get("course")); }
        if (p.get("platform"))  { clauses.push("platform = ?");             binds.push(p.get("platform")); }
        // Numeric params are validated up front: Number("abc") is NaN, and a
        // NaN bind makes D1 throw — turning a caller's typo into a 500.
        const nums = {};
        for (const key of ["max_price", "min_spots", "limit"]) {
          const raw = p.get(key);
          if (raw === null || raw === "") continue;
          const n = Number(raw);
          if (!Number.isFinite(n)) {
            return json({ error: `${key} must be a number, got ${JSON.stringify(raw)}` }, 400);
          }
          nums[key] = n;
        }
        if (nums.max_price !== undefined) { clauses.push("price_min <= ?"); binds.push(nums.max_price); }
        if (nums.min_spots !== undefined) { clauses.push("open_spots >= ?"); binds.push(nums.min_spots); }
        // Freshness guard: hide slots from a stalled scraper (see freshnessFilter).
        { const f = freshnessFilter(); clauses.push(f.clause); binds.push(...f.binds); }
        // Clamp to [1, 25000]. The ceiling stops ?limit=-1 (SQLite reads a
        // negative limit as "no limit") from dumping the whole table, but is
        // high enough that a dense platform's full day (teeitup across all 5
        // covered states ~10k rows) is returned complete instead of being cut
        // off mid-morning — the frontend was silently losing every afternoon
        // slot at the old 2000 cap. Actual rows returned is still min(matching,
        // limit), so a scoped query stays small.
        const limit = Math.min(Math.max(Math.trunc(nums.limit ?? 500), 1), 25000);

        // Dedupe by (venue, teetime, sub-course): when the native engine and
        // its GolfNow overflow both list a slot, keep the primary source's row
        // (its native booking link). Different sub-courses at the same time are
        // NOT duplicates. LIMIT applies AFTER dedup.
        const { results } = await env.DB.prepare(
          `WITH filtered AS (
             SELECT *, COALESCE(venue_id, course_slug) AS vid,
                    COALESCE(course_label, '') AS clabel
               FROM tee_times
              WHERE ${clauses.join(" AND ")}
           ),
           ranked AS (
             SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY vid, teetime, clabel
                      ORDER BY (CASE WHEN source_role = 'primary' THEN 0 ELSE 1 END),
                               price_min
                    ) AS rn
               FROM filtered
           )
           SELECT ranked.*, g.lat AS lat, g.lng AS lng FROM ranked LEFT JOIN venue_geo g ON g.venue_id = ranked.vid WHERE rn = 1
            ORDER BY teetime LIMIT ?`).bind(...binds, limit).all();

        // Present venue as the course id and the sub-course in the name.
        for (const r of results) {
          r.course_slug = r.vid || r.course_slug;
          r.course_label = r.clabel || "";
          r.course_name = displayName(r.course_name, r.course_label);
          delete r.vid;
          delete r.clabel;
          delete r.rn;
        }
        // `truncated` tells the caller this page hit LIMIT — `count` is the
        // page's length, NOT the total; without the flag a capped response is
        // indistinguishable from a complete one (afternoon slots silently
        // vanish for busy dates).
        return json({ count: results.length,
                      truncated: results.length === limit,
                      tee_times: results });
      }

      return json({ error: "not found",
                    routes: ["/api/health", "/api/courses", "/api/tee-times",
                             "/api/directory", "/api/revalidate"] }, 404);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },

  // Cron trigger (see wrangler.toml [triggers]) — deactivate rows whose tee
  // time has already elapsed in the course's own timezone.
  //
  // The read filter above already hides these, so this is about the DATA rather
  // than the site: anything else reading D1 (exports, the OneTee post job, ad
  // hoc queries, a future second consumer) sees the truth too, and `active`
  // stops drifting upward forever.
  //
  // This lives in the Worker rather than in GitHub Actions because Actions
  // could not be relied on to run it. The equivalent workflow with a */10 cron
  // was on main for over an hour without firing once, and the */5 fast scrape
  // actually executes roughly every five HOURS — GitHub's scheduler is
  // best-effort and deprioritises frequent crons under load. Cloudflare's runs
  // on time, has the D1 binding already, and costs nothing.
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const stmts = [];
      const allStates = Object.keys(STATE_TZ);
      for (const [tz, states] of Object.entries(tzGroups())) {
        const marks = states.map(() => "?").join(",");
        // Panhandle FL is Central — pruned by its own statement below, and it
        // must NOT be pruned an hour early by the Eastern group here.
        const carve = tz === "America/New_York"
          ? ` AND NOT (${FL_CENTRAL_ARM})` : "";
        stmts.push(env.DB.prepare(
          `UPDATE tee_times SET active = 0
            WHERE active = 1 AND state IN (${marks}) AND teetime < ?${carve}`)
          .bind(...states, localNowISO(tz)));
      }
      stmts.push(env.DB.prepare(
        `UPDATE tee_times SET active = 0
          WHERE active = 1 AND ${FL_CENTRAL_ARM} AND teetime < ?`)
        .bind(localNowISO("America/Chicago")));
      // Fallback covers blank AND unrecognized states (e.g. 'PR', a typo, a
      // lowercase code): without the NOT IN arm those rows were never pruned
      // and `active` drifted upward forever.
      stmts.push(env.DB.prepare(
        `UPDATE tee_times SET active = 0
          WHERE active = 1 AND (state IS NULL OR state = ''
                OR state NOT IN (${allStates.map((s) => `'${s}'`).join(",")}))
            AND teetime < ?`)
        .bind(localNowISO(FALLBACK_TZ)));
      const res = await env.DB.batch(stmts);
      const n = res.reduce((a, r) => a + (r.meta?.changes || 0), 0);
      console.log(`prune: deactivated ${n} elapsed rows`);
    })());
  },
};
