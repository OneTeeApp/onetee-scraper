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
 * sees ONE course per venue with no changes needed. venue_id falls back to
 * course_slug for legacy rows that predate the column.
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

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);

    try {
      if (url.pathname === "/api/health") {
        const r = await env.DB.prepare(
          "SELECT COUNT(*) AS total, SUM(active) AS active FROM tee_times").first();
        return json({ ok: true, ...r });
      }

      if (url.pathname === "/api/courses") {
        // One row per physical venue. Prefer the primary (native) source's
        // platform + booking link; count distinct times so a slot listed by
        // both the native engine and GolfNow isn't double-counted.
        const p = url.searchParams;
        const clauses = ["active = 1"];
        const binds = [];
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
                  COUNT(DISTINCT teetime) AS slots,
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

        // Dedupe by (venue_id, teetime): when the native engine and its GolfNow
        // overflow both list the same slot, keep the primary source's row (its
        // native booking link). LIMIT applies AFTER dedup so it can't slice off
        // a primary and leave its duplicate supplement.
        const { results } = await env.DB.prepare(
          `WITH filtered AS (
             SELECT *, COALESCE(venue_id, course_slug) AS vid
               FROM tee_times
              WHERE ${clauses.join(" AND ")}
           ),
           ranked AS (
             SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY vid, teetime
                      ORDER BY (CASE WHEN source_role = 'primary' THEN 0 ELSE 1 END),
                               price_min
                    ) AS rn
               FROM filtered
           )
           SELECT * FROM ranked WHERE rn = 1
            ORDER BY teetime LIMIT ?`).bind(...binds, limit).all();

        // Present the venue as the course id and drop query helpers.
        for (const r of results) {
          r.course_slug = r.vid || r.course_slug;
          delete r.vid;
          delete r.rn;
        }
        return json({ count: results.length, tee_times: results });
      }

      return json({ error: "not found", routes: ["/api/health", "/api/courses", "/api/tee-times"] }, 404);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};
