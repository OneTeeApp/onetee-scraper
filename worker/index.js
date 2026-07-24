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

const CORS = {
  "Access-Control-Allow-Origin": "*", // tighten to https://www.oneteeapp.com later
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
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
// actual filters. Inlining keeps it at 8 binds: one clock per zone.
const TZ_ORDER = Object.entries(tzGroups());
const PAST_CLAUSE = `teetime >= CASE ${TZ_ORDER
  .map(([, states]) => `WHEN state IN (${states.map((s) => `'${s}'`).join(",")}) THEN ?`)
  .join(" ")} ELSE ? END`;
const pastFilter = () => ({
  clause: PAST_CLAUSE,
  binds: [...TZ_ORDER.map(([tz]) => localNowISO(tz)), localNowISO(FALLBACK_TZ)],
});

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

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);

    const { clause: pastClause, binds: pastBinds } = pastFilter();

    try {
      if (url.pathname === "/api/health") {
        const r = await env.DB.prepare(
          "SELECT COUNT(*) AS total, SUM(active) AS active FROM tee_times").first();
        return json({ ok: true, ...r });
      }

      if (url.pathname === "/api/courses") {
        // One row per physical venue. Prefer the primary (native) source's
        // platform + booking link; count distinct upcoming times so a slot
        // listed by both the native engine and GolfNow isn't double-counted.
        const p = url.searchParams;
        const clauses = ["active = 1"];
        const binds = [];
        if (!p.get("include_past")) { clauses.push(pastClause); binds.push(...pastBinds); }
        if (p.get("state")) { clauses.push("state = ?");            binds.push(p.get("state").toUpperCase()); }
        if (p.get("city"))  { clauses.push("LOWER(city) = LOWER(?)"); binds.push(p.get("city")); }
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
        if (!p.get("include_past")) { clauses.push(pastClause); binds.push(...pastBinds); }
        if (p.get("date"))      { clauses.push("substr(teetime,1,10) = ?"); binds.push(p.get("date")); }
        if (p.get("state"))     { clauses.push("state = ?");                binds.push(p.get("state").toUpperCase()); }
        if (p.get("city"))      { clauses.push("LOWER(city) = LOWER(?)");   binds.push(p.get("city")); }
        // course filter is venue-aware: accepts a venue_id (what this API now
        // hands out as course_slug) or a legacy source slug.
        if (p.get("course"))    { clauses.push("COALESCE(venue_id, course_slug) = ?"); binds.push(p.get("course")); }
        if (p.get("platform"))  { clauses.push("platform = ?");             binds.push(p.get("platform")); }
        if (p.get("max_price")) { clauses.push("price_min <= ?");           binds.push(Number(p.get("max_price"))); }
        if (p.get("min_spots")) { clauses.push("open_spots >= ?");          binds.push(Number(p.get("min_spots"))); }
        const limit = Math.min(Number(p.get("limit") || 500), 2000);

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
           SELECT * FROM ranked WHERE rn = 1
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
        return json({ count: results.length, tee_times: results });
      }

      return json({ error: "not found", routes: ["/api/health", "/api/courses", "/api/tee-times"] }, 404);
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
      for (const [tz, states] of Object.entries(tzGroups())) {
        const marks = states.map(() => "?").join(",");
        stmts.push(env.DB.prepare(
          `UPDATE tee_times SET active = 0
            WHERE active = 1 AND state IN (${marks}) AND teetime < ?`)
          .bind(...states, localNowISO(tz)));
      }
      stmts.push(env.DB.prepare(
        `UPDATE tee_times SET active = 0
          WHERE active = 1 AND (state IS NULL OR state = '') AND teetime < ?`)
        .bind(localNowISO(FALLBACK_TZ)));
      const res = await env.DB.batch(stmts);
      const n = res.reduce((a, r) => a + (r.meta?.changes || 0), 0);
      console.log(`prune: deactivated ${n} elapsed rows`);
    })());
  },
};
