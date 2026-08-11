// OneTee read + ingest API (Node) — runs on the VPS in front of Postgres.
// The READ handlers reuse the EXACT SQL + filters from the Cloudflare worker
// (worker/index.js) through a tiny D1-compatible shim, so behaviour matches the
// current site. Adds POST /ingest for the scrapers to write to.
import http from "node:http";
import { readFileSync } from "node:fs";
import pg from "pg";

// Postgres returns bigint (int8: COUNT/SUM, BIGSERIAL ids) as strings by default.
// D1/SQLite returns them as numbers, and the scraper does arithmetic on counts
// (e.g. `total += n`) once it reads through /exec in VPS-only mode. Parse int8 as
// a JS number to match D1 — all our int8 values are far below 2^53.
pg.types.setTypeParser(20, (v) => (v === null ? null : Number(v)));

const PORT = Number(process.env.PORT || 8080);
const INGEST_TOKEN = process.env.INGEST_TOKEN || "";
const DIRECTORY_PATH = process.env.DIRECTORY_PATH || "/opt/onetee-api/directory.json";

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 16 });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Postgres SQLSTATEs worth retrying: deadlock_detected, serialization_failure,
// lock_not_available (our lock_timeout firing). The scrapers' broad prune
// UPDATEs (SET active=0 WHERE ... teetime < now) lock large overlapping row
// sets, so once the whole fleet dual-writes concurrently they deadlock.
const RETRYABLE = new Set(["40P01", "40001", "55P03"]);

// ---- D1-compatible shim: prepare().bind().all()/first()/run() over Postgres,
// converting SQLite '?' placeholders to Postgres $1,$2,... in order. ----
const toPg = (sql) => { let i = 0; return sql.replace(/\?/g, () => "$" + (++i)); };
const DB = {
  prepare(sql) {
    const pgsql = toPg(sql);
    const stmt = {
      _p: [],
      bind(...a) { stmt._p = a; return stmt; },
      async all() { const r = await pool.query(pgsql, stmt._p); return { results: r.rows }; },
      async first() { const r = await pool.query(pgsql, stmt._p); return r.rows[0] || null; },
      async run() { await pool.query(pgsql, stmt._p); return { success: true }; },
    };
    return stmt;
  },
};

// ---- Static directory bundle (course metadata) ----
let DIRECTORY = { courses: [] };
try {
  const parsed = JSON.parse(readFileSync(DIRECTORY_PATH, "utf8"));
  DIRECTORY = Array.isArray(parsed) ? { courses: parsed } : parsed;
} catch (e) { console.error("directory load failed:", e.message); }

// ===================== helpers copied from worker/index.js ==================
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};
const localNowISO = (tz) =>
  new Date().toLocaleString("sv-SE", { timeZone: tz }).replace(" ", "T");

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
  AZ: "America/Phoenix",
  CA: "America/Los_Angeles", NV: "America/Los_Angeles",
  OR: "America/Los_Angeles", WA: "America/Los_Angeles",
  AK: "America/Anchorage", HI: "Pacific/Honolulu",
};
const FL_CENTRAL_CITIES = [
  "Bonifay", "Crestview", "DeFuniak Springs", "Destin", "Fort Walton Beach", "Freeport",
  "Gulf Breeze", "Hurlburt Field", "Lynn Haven", "Milton", "Miramar Beach",
  "Navarre", "Niceville", "Pace", "Panama City", "Panama City Beach",
  "Pensacola", "Shalimar", "Sunny Hills", "Watersound",
];
const FL_CENTRAL_SQL = FL_CENTRAL_CITIES.map((c) => `'${c}'`).join(",");
const FL_CENTRAL_ARM = `state = 'FL' AND COALESCE(city,'') IN (${FL_CENTRAL_SQL})`;
const FALLBACK_TZ = "Pacific/Honolulu";
const tzGroups = () => {
  const g = {};
  for (const [st, tz] of Object.entries(STATE_TZ)) (g[tz] ||= []).push(st);
  return g;
};
const TZ_ORDER = Object.entries(tzGroups());
const PAST_CLAUSE = `teetime >= CASE WHEN ${FL_CENTRAL_ARM} THEN ? ${TZ_ORDER
  .map(([, states]) => `WHEN state IN (${states.map((s) => `'${s}'`).join(",")}) THEN ?`)
  .join(" ")} ELSE ? END`;
const pastFilter = () => ({
  clause: PAST_CLAUSE,
  binds: [localNowISO("America/Chicago"),
          ...TZ_ORDER.map(([tz]) => localNowISO(tz)), localNowISO(FALLBACK_TZ)],
});
const wantsPast = (p) => {
  const v = (p.get("include_past") || "").toLowerCase();
  return v !== "" && v !== "0" && v !== "false" && v !== "no";
};
const displayName = (name, label) => {
  if (!label) return name;
  const words = new Set(
    (name || "").toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 3));
  const shares = (label.toLowerCase().split(/[^a-z0-9]+/) || [])
    .some((w) => words.has(w));
  return shares ? label : `${name} · ${label}`;
};

const FRESH_TODAY_DAYS = 0, FRESH_TODAY_MIN = 3 * 60;
const FRESH_NEAR_DAYS = 2, FRESH_NEAR_MIN = 6 * 60;
const FRESH_MID_DAYS = 7, FRESH_MID_MIN = 18 * 60;
const FRESH_FAR_MIN = 30 * 60;
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
  const todayBoundary = boundary(FRESH_TODAY_DAYS);
  const nearBoundary = boundary(FRESH_NEAR_DAYS);
  const midBoundary = boundary(FRESH_MID_DAYS);
  const todayCut = utc19(now - FRESH_TODAY_MIN * 60000);
  const nearCut = utc19(now - FRESH_NEAR_MIN * 60000);
  const midCut = utc19(now - FRESH_MID_MIN * 60000);
  const farCut = utc19(now - FRESH_FAR_MIN * 60000);
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

let GEO_CACHE = null;
async function venueGeo() {
  if (GEO_CACHE) return GEO_CACHE;
  const m = new Map();
  try {
    const { results } = await DB.prepare("SELECT venue_id, lat, lng FROM venue_geo").all();
    for (const r of results) if (r.lat != null && r.lng != null) m.set(r.venue_id, [r.lat, r.lng]);
  } catch (e) { /* no geo -> nulls */ }
  GEO_CACHE = m;
  return m;
}

// ===================== HTTP plumbing =====================
const send = (res, status, obj, extra = {}) => {
  res.writeHead(status, { "Content-Type": "application/json", ...CORS, ...extra });
  res.end(JSON.stringify(obj));
};
const readBody = (req) => new Promise((resolve, reject) => {
  let d = ""; req.on("data", (c) => { d += c; if (d.length > 60e6) req.destroy(); });
  req.on("end", () => resolve(d)); req.on("error", reject);
});

// ===================== handlers =====================
async function health() {
  let r = null;
  try {
    r = await DB.prepare(
      "SELECT generated_at, date, tee_times, courses_ok, courses_queried " +
      "FROM runs ORDER BY id DESC LIMIT 1").first();
  } catch (e) { /* runs empty */ }
  return { status: 200, body: { ok: true, last_run: r || null },
           headers: { "Cache-Control": "public, max-age=60" } };
}

async function directory(p) {
  const st = (p.get("state") || "").toUpperCase();
  const method = (p.get("method") || "").toLowerCase();
  const city = (p.get("city") || "").toLowerCase();
  const q = (p.get("q") || "").toLowerCase();
  let courses = DIRECTORY.courses || [];
  if (st) courses = courses.filter((c) => c.state === st);
  if (method) courses = courses.filter((c) => c.booking_method === method);
  if (city) courses = courses.filter((c) => (c.city || "").toLowerCase() === city);
  if (q) courses = courses.filter((c) => (c.name || "").toLowerCase().includes(q));
  const geo = await venueGeo();
  courses = courses.map((c) => {
    const g = geo.get(c.venue_id);
    return { ...c, lat: g ? g[0] : null, lng: g ? g[1] : null };
  });
  return { status: 200, body: { count: courses.length, courses },
           headers: { "Cache-Control": "public, max-age=3600, s-maxage=86400" } };
}

async function courses(p) {
  const clauses = ["active = 1"];
  const binds = [];
  if (!wantsPast(p)) { const f = pastFilter(); clauses.push(f.clause); binds.push(...f.binds); }
  if (p.get("state")) { clauses.push("state = ?"); binds.push(p.get("state").toUpperCase()); }
  if (p.get("city")) { clauses.push("LOWER(city) = LOWER(?)"); binds.push(p.get("city")); }
  { const f = freshnessFilter(); clauses.push(f.clause); binds.push(...f.binds); }
  const { results } = await DB.prepare(
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
  return { status: 200, body: { courses: results } };
}

async function teeTimes(p) {
  const clauses = ["active = 1"];
  const binds = [];
  if (!wantsPast(p)) { const f = pastFilter(); clauses.push(f.clause); binds.push(...f.binds); }
  if (p.get("date")) { clauses.push("substr(teetime,1,10) = ?"); binds.push(p.get("date")); }
  if (p.get("state")) {
    const _st = p.get("state").toUpperCase().split(",").map((s) => s.trim()).filter(Boolean);
    if (_st.length === 1) { clauses.push("state = ?"); binds.push(_st[0]); }
    else if (_st.length) { clauses.push("state IN (" + _st.map(() => "?").join(",") + ")"); binds.push(..._st); }
  }
  if (p.get("city")) { clauses.push("LOWER(city) = LOWER(?)"); binds.push(p.get("city")); }
  if (p.get("course")) { clauses.push("COALESCE(venue_id, course_slug) = ?"); binds.push(p.get("course")); }
  if (p.get("platform")) { clauses.push("platform = ?"); binds.push(p.get("platform")); }
  const nums = {};
  for (const key of ["max_price", "min_spots", "limit"]) {
    const raw = p.get(key);
    if (raw === null || raw === "") continue;
    const n = Number(raw);
    if (!Number.isFinite(n)) return { status: 400, body: { error: `${key} must be a number` } };
    nums[key] = n;
  }
  if (nums.max_price !== undefined) { clauses.push("price_min <= ?"); binds.push(nums.max_price); }
  if (nums.min_spots !== undefined) { clauses.push("open_spots >= ?"); binds.push(nums.min_spots); }
  { const f = freshnessFilter(); clauses.push(f.clause); binds.push(...f.binds); }
  const limit = Math.min(Math.max(Math.trunc(nums.limit ?? 500), 1), 25000);

  const { results } = await DB.prepare(
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

  for (const r of results) {
    r.course_slug = r.vid || r.course_slug;
    r.course_label = r.clabel || "";
    r.course_name = displayName(r.course_name, r.course_label);
    delete r.vid; delete r.clabel; delete r.rn;
  }
  return { status: 200, body: { count: results.length, truncated: results.length === limit, tee_times: results } };
}

// POST /ingest — bearer-token write path for scrapers. Upserts tee_times rows,
// optional freshness stamps, and an optional run summary. (v1 contract; the
// scraper HttpIngest backend will target this.)
const TT_COLS = ["course_slug","teetime","course_label","course_name","city","state",
  "venue_id","source_role","platform","holes","open_spots","price_min","price_max",
  "currency","booking_url","simulated","active","first_seen_at","last_seen_at"];
async function ingest(req, res) {
  const auth = req.headers["authorization"] || "";
  if (!INGEST_TOKEN || auth !== `Bearer ${INGEST_TOKEN}`) return send(res, 401, { error: "unauthorized" });
  let payload;
  try { payload = JSON.parse(await readBody(req)); }
  catch (e) { return send(res, 400, { error: "bad json" }); }
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const fresh = Array.isArray(payload.freshness) ? payload.freshness : [];
  const nowIso = new Date().toISOString();
  const client = await pool.connect();
  let inserted = 0;
  try {
    await client.query("BEGIN");
    for (const r of rows) {
      const vals = [
        r.course_slug, r.teetime, r.course_label ?? "", r.course_name ?? "",
        r.city ?? null, r.state ?? null, r.venue_id ?? null, r.source_role ?? "primary",
        r.platform ?? null, r.holes ?? null, r.open_spots ?? null, r.price_min ?? null,
        r.price_max ?? null, r.currency ?? "USD", r.booking_url ?? null,
        r.simulated ?? 0, r.active ?? 1, r.first_seen_at ?? nowIso, r.last_seen_at ?? nowIso,
      ];
      const ph = vals.map((_, i) => "$" + (i + 1)).join(",");
      await client.query(
        `INSERT INTO tee_times (${TT_COLS.join(",")}) VALUES (${ph})
         ON CONFLICT (course_slug, teetime, course_label) DO UPDATE SET
           course_name=EXCLUDED.course_name, city=EXCLUDED.city, state=EXCLUDED.state,
           venue_id=EXCLUDED.venue_id, source_role=EXCLUDED.source_role,
           platform=EXCLUDED.platform, holes=EXCLUDED.holes, open_spots=EXCLUDED.open_spots,
           price_min=EXCLUDED.price_min, price_max=EXCLUDED.price_max,
           currency=EXCLUDED.currency, booking_url=EXCLUDED.booking_url,
           simulated=EXCLUDED.simulated, active=EXCLUDED.active,
           last_seen_at=EXCLUDED.last_seen_at`, vals);
      inserted++;
    }
    for (const f of fresh) {
      await client.query(
        `INSERT INTO sheet_freshness (course_slug, date, last_ok_at) VALUES ($1,$2,$3)
         ON CONFLICT (course_slug, date) DO UPDATE SET last_ok_at=EXCLUDED.last_ok_at`,
        [f.course_slug, f.date, f.last_ok_at]);
    }
    if (payload.run) {
      const s = payload.run;
      await client.query(
        `INSERT INTO runs (generated_at,date,courses_queried,courses_ok,tee_times,
           rows_inserted,rows_updated,rows_deactivated,errors)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
        [s.generated_at ?? nowIso, s.date ?? "", s.courses_queried ?? null,
         s.courses_ok ?? null, s.tee_times ?? rows.length, s.rows_inserted ?? inserted,
         s.rows_updated ?? 0, s.rows_deactivated ?? 0, JSON.stringify(s.errors ?? [])]);
    }
    await client.query("COMMIT");
  } catch (e) {
    await client.query("ROLLBACK");
    return send(res, 500, { error: String(e.message || e) });
  } finally { client.release(); }
  GEO_CACHE = null; // in case geo changed
  return send(res, 200, { ok: true, upserted: inserted, freshness: fresh.length });
}

// POST /exec — bearer-token SQL execution for the scraper's future HttpBackend
// (mirrors Cloudflare D1's REST API, so scraper/d1.py's sync() can run unchanged
// against Postgres). Translates the one SQLite-dialect gap in the write path —
// "INSERT OR REPLACE INTO <table>" -> INSERT ... ON CONFLICT DO UPDATE — and
// converts ? placeholders to $n. Everything else in d1.py (SELECT substr(),
// UPDATE, INSERT ... ON CONFLICT for freshness, INSERT INTO runs) is already
// Postgres-compatible. Token-gated; only the scraper calls it.
const PK = {
  tee_times: ["course_slug", "teetime", "course_label"],
  sheet_freshness: ["course_slug", "date"],
};
function translate(sql, params) {
  let s = String(sql).trim();
  // INSERT OR REPLACE INTO <t> (...) -> ON CONFLICT (pk) DO UPDATE (scraper tee tables)
  const m = s.match(/^INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)([\s\S]*)$/i);
  if (m) {
    const [, tbl, cols, rest] = m;
    const pk = PK[tbl] || [];
    const upd = cols.split(",").map((c) => c.trim())
      .filter((c) => !pk.includes(c)).map((c) => `${c}=EXCLUDED.${c}`).join(", ");
    s = `INSERT INTO ${tbl} (${cols})${rest} ON CONFLICT (${pk.join(",")}) DO UPDATE SET ${upd}`;
  }
  // INSERT OR IGNORE INTO ... -> INSERT INTO ... ON CONFLICT DO NOTHING (gate billing_events)
  const g = s.match(/^INSERT\s+OR\s+IGNORE\s+INTO\s+([\s\S]+)$/i);
  if (g) s = `INSERT INTO ${g[1]} ON CONFLICT DO NOTHING`;
  // Placeholders: the gate uses numbered ?1/?2 (keep the number -> $1/$2); the
  // scraper uses bare ? (assign sequential $1,$2,...). Never both in one statement.
  if (/\?\d/.test(s)) {
    s = s.replace(/\?(\d+)/g, (_, n) => "$" + n);
  } else {
    let i = 0;
    s = s.replace(/\?/g, () => "$" + (++i));
  }
  return { text: s, values: params || [] };
}
async function exec(req, res) {
  const auth = req.headers["authorization"] || "";
  if (!INGEST_TOKEN || auth !== `Bearer ${INGEST_TOKEN}`) return send(res, 401, { error: "unauthorized" });
  let payload;
  try { payload = JSON.parse(await readBody(req)); } catch (e) { return send(res, 400, { error: "bad json" }); }
  const stmts = Array.isArray(payload.batch) ? payload.batch
    : [{ sql: payload.sql, params: payload.params || [] }];

  const MAX_ATTEMPTS = 5;
  let lastErr;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      // Fail fast on lock contention (retry below) instead of pinning a
      // connection until a deadlock resolves; cap runaway statements too.
      await client.query("SET LOCAL lock_timeout = '4s'");
      await client.query("SET LOCAL statement_timeout = '25s'");
      let results = [];
      let changes = 0;
      for (const st of stmts) {
        if (!st || !st.sql) continue;
        // Skip DDL — the VPS schema is deploy-managed, so the gate's ensureSchema()
        // (CREATE TABLE ... in SQLite dialect) is a harmless no-op here.
        if (/^\s*(CREATE|ALTER|DROP)\b/i.test(st.sql)) continue;
        const { text, values } = translate(st.sql, st.params || []);
        const r = await client.query(text, values);
        results = r.rows;
        changes = r.rowCount ?? 0;
      }
      await client.query("COMMIT");
      GEO_CACHE = null;
      // `changes` = rowCount of the last statement, so the gate's D1 shim can
      // expose `.meta.changes` (used for its billing-event dedup check).
      return send(res, 200, { results, changes });
    } catch (e) {
      try { await client.query("ROLLBACK"); } catch (_) { /* connection already gone */ }
      lastErr = e;
      const code = e && e.code;
      if (RETRYABLE.has(code) && attempt < MAX_ATTEMPTS) {
        await sleep(60 * attempt + Math.floor(Math.random() * 50)); // jittered backoff
        continue; // retry the whole batch on a fresh connection
      }
      console.error(`/exec failed code=${code || "?"} attempt=${attempt}: ${String(e.message || e)} :: ` +
        stmts.map((s) => String(s && s.sql).slice(0, 140)).join(" | "));
      return send(res, 500, { error: String(e.message || e), code: code || null });
    } finally {
      client.release();
    }
  }
  return send(res, 500, { error: String((lastErr && lastErr.message) || lastErr || "exec failed") });
}

// ===================== router =====================
const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "OPTIONS") { res.writeHead(204, CORS); return res.end(); }
    const url = new URL(req.url, "http://x");
    const p = url.searchParams;
    if (req.method === "POST" && url.pathname === "/ingest") return ingest(req, res);
    if (req.method === "POST" && url.pathname === "/exec") return exec(req, res);
    let out;
    if (url.pathname === "/api/health") out = await health();
    else if (url.pathname === "/api/directory") out = await directory(p);
    else if (url.pathname === "/api/courses") out = await courses(p);
    else if (url.pathname === "/api/tee-times") out = await teeTimes(p);
    else return send(res, 404, { error: "not found" });
    return send(res, out.status, out.body, out.headers || {});
  } catch (e) {
    console.error("handler error:", e);
    return send(res, 500, { error: String(e.message || e) });
  }
});
server.listen(PORT, "127.0.0.1", () => console.log(`onetee-api listening on 127.0.0.1:${PORT}`));
