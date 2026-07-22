/**
 * OneTee read API — a Cloudflare Worker in front of the D1 database.
 * Deploy with:  cd worker && npx wrangler deploy
 * Then your site can call e.g.
 *   GET https://onetee-api.<you>.workers.dev/api/tee-times?date=2026-07-25&city=Denver&max_price=80
 *   GET .../api/courses
 *   GET .../api/health
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
        const { results } = await env.DB.prepare(
          `SELECT course_slug, course_name, city, platform, booking_url,
                  COUNT(*) AS slots, MIN(price_min) AS from_price
             FROM tee_times WHERE active = 1
             GROUP BY course_slug ORDER BY course_name`).all();
        return json({ courses: results });
      }

      if (url.pathname === "/api/tee-times") {
        const p = url.searchParams;
        const clauses = ["active = 1"];
        const binds = [];
        if (p.get("date"))      { clauses.push("substr(teetime,1,10) = ?"); binds.push(p.get("date")); }
        if (p.get("city"))      { clauses.push("LOWER(city) = LOWER(?)");   binds.push(p.get("city")); }
        if (p.get("course"))    { clauses.push("course_slug = ?");          binds.push(p.get("course")); }
        if (p.get("platform"))  { clauses.push("platform = ?");             binds.push(p.get("platform")); }
        if (p.get("max_price")) { clauses.push("price_min <= ?");           binds.push(Number(p.get("max_price"))); }
        if (p.get("min_spots")) { clauses.push("open_spots >= ?");          binds.push(Number(p.get("min_spots"))); }
        const limit = Math.min(Number(p.get("limit") || 500), 2000);

        const { results } = await env.DB.prepare(
          `SELECT * FROM tee_times WHERE ${clauses.join(" AND ")}
             ORDER BY teetime LIMIT ?`).bind(...binds, limit).all();
        return json({ count: results.length, tee_times: results });
      }

      return json({ error: "not found", routes: ["/api/health", "/api/courses", "/api/tee-times"] }, 404);
    } catch (e) {
      return json({ error: String(e) }, 500);
    }
  },
};
