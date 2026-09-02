// OneTee — static, indexable tee-time pages for tee-times.oneteeapp.com
//
// Runs on the VPS (systemd timer, every 2 hours; see vps/deploy.sh). Reads
// today's tee times from the local API (so it inherits the exact "not in the
// past / sheet still fresh / one row per slot" rules the widget uses) and the
// course directory, then writes a plain HTML site that Caddy serves as files:
//
//   /                          index: the states we cover, with today's counts
//   /<state>/                  every course in the state, today's times first
//   /<state>/<city>/           the courses in one city
//   /course/<venue_id>/        one course: today's times, phone, website, book link
//   /sitemap.xml  /robots.txt  /404.html
//
// Pages are written to a temp directory and swapped in atomically, so a crawl
// never sees a half-built site. No secrets, no auth: the API on 127.0.0.1 is
// the same data the public site serves to signed-out visitors.
//
// From 6 pm local time a state's pages show tomorrow's sheet instead of the
// dregs of today's (ROLL_HOUR). Private clubs, courses with no way to reach them
// and one-course city pages are written but carry noindex,follow and stay out
// of the sitemap, so what Google indexes is the useful part.
//
// Env: API_BASE (default http://127.0.0.1:8080), PAGES_OUT (default
// /var/www/onetee-pages), PAGES_HOST (default tee-times.oneteeapp.com),
// ROLL_HOUR (default 18), FIXTURE=1 renders from the bundled directory with
// synthetic times (local dev).

import http from "node:http";
import https from "node:https";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const API = process.env.API_BASE || "http://127.0.0.1:8080";
const OUT = process.env.PAGES_OUT || "/var/www/onetee-pages";
const HOST = process.env.PAGES_HOST || "tee-times.oneteeapp.com";
const SITE = "https://" + HOST;
const MAIN = "https://www.oneteeapp.com";
// Site logo (Squarespace Social Sharing image, 528x402). Default og:image + Organization logo.
const LOGO = "https://static1.squarespace.com/static/6a614108f62a98651c8be736/t/6a9857b3b2eb0270855334a9/1788368819212/IMG_6529.jpeg?format=1500w";
const ORG_LD = { "@type": "Organization", "@id": MAIN + "/#organization", name: "OneTee", url: MAIN + "/", logo: { "@type": "ImageObject", url: LOGO, width: 528, height: 402 } };
const FIXTURE = process.env.FIXTURE === "1";
const DIRECTORY_PATH = process.env.DIRECTORY_PATH || "/opt/onetee-api/directory.json";

const STATE_NAMES = {
  AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
  CT: "Connecticut", DE: "Delaware", DC: "Washington, DC", FL: "Florida", GA: "Georgia", HI: "Hawaii",
  ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana",
  ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi",
  MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey",
  NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
  OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
  TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington",
  WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
};
// Same map as vps/api/server.mjs — "today" is the course's day, not the server's.
const STATE_TZ = {
  CT: "America/New_York", DE: "America/New_York", FL: "America/New_York", GA: "America/New_York",
  IN: "America/New_York", KY: "America/New_York", ME: "America/New_York", MD: "America/New_York",
  MA: "America/New_York", MI: "America/New_York", NH: "America/New_York", NJ: "America/New_York",
  NY: "America/New_York", NC: "America/New_York", OH: "America/New_York", PA: "America/New_York",
  RI: "America/New_York", SC: "America/New_York", VT: "America/New_York", VA: "America/New_York",
  WV: "America/New_York", DC: "America/New_York",
  AL: "America/Chicago", AR: "America/Chicago", IL: "America/Chicago", IA: "America/Chicago",
  KS: "America/Chicago", LA: "America/Chicago", MN: "America/Chicago", MS: "America/Chicago",
  MO: "America/Chicago", NE: "America/Chicago", ND: "America/Chicago", OK: "America/Chicago",
  SD: "America/Chicago", TN: "America/Chicago", TX: "America/Chicago", WI: "America/Chicago",
  CO: "America/Denver", MT: "America/Denver", NM: "America/Denver", UT: "America/Denver",
  WY: "America/Denver", ID: "America/Denver", AZ: "America/Phoenix",
  CA: "America/Los_Angeles", NV: "America/Los_Angeles", OR: "America/Los_Angeles", WA: "America/Los_Angeles",
  AK: "America/Anchorage", HI: "Pacific/Honolulu",
};
const tzOf = (st) => STATE_TZ[st] || "America/Denver";
const localDate = (tz, d = new Date()) => d.toLocaleString("sv-SE", { timeZone: tz }).slice(0, 10);
const localClock = (tz, d = new Date()) =>
  d.toLocaleTimeString("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const localHour = (tz, d = new Date()) => Number(d.toLocaleString("en-US", { timeZone: tz, hour: "numeric", hourCycle: "h23" }));

// Which day a state's pages show. From ROLL_HOUR (6 pm local) the day's sheet is
// mostly history, so the pages roll to tomorrow: an evening visitor — or a crawler
// that happens to come by at night — sees a full sheet instead of "0 times".
const ROLL_HOUR = Number(process.env.ROLL_HOUR || 18);
const DAY_CACHE = {};
function dayFor(st) {
  if (DAY_CACHE[st]) return DAY_CACHE[st];
  const tz = tzOf(st), now = new Date(), today = localDate(tz, now);
  const rolled = localHour(tz, now) >= ROLL_HOUR;
  const date = rolled ? localDate(tz, new Date(now.getTime() + 86400000)) : today;
  return (DAY_CACHE[st] = { date, today, rolled, word: rolled ? "tomorrow" : "today", Word: rolled ? "Tomorrow" : "Today" });
}

// Booking platforms as people know them (directory slugs -> names). "other:*" stays unnamed.
const PLATFORM_NAMES = {
  teeitup: "TeeItUp", foreup: "ForeUp", chronogolf: "Chronogolf", golfnow: "GolfNow", ezlinks: "EZLinks",
  clubprophet: "Club Prophet", teesnap: "Teesnap", clubcaddie: "Club Caddie", membersports: "MemberSports",
  quick18: "Quick18", golfwithaccess: "Golf With Access", golfback: "GolfBack", tenfore: "TenFore", rguest: "rGuest",
  courseco: "CourseCo", teequest: "TeeQuest", agilysys: "Agilysys", trutee: "TruTee", supersaas: "SuperSaaS",
};
// Private and military courses have no public tee times; their pages exist for the
// person who searches the name, but they are not what the site is about.
const isPublic = (c) => c.booking_method !== "private" && c.booking_method !== "military";
// Pages worth asking Google to index: a public course we can actually help you reach,
// and a city with at least two of them (a one-course city page just repeats the course page).
const courseIndexable = (c) => isPublic(c) && !!(c.phone || c.website || c.action_url);
const cityIndexable = (x) => x.courses.filter(courseIndexable).length >= 2;
const listNames = (arr) => arr.length <= 1 ? arr.join("") : arr.slice(0, -1).join(", ") + " and " + arr[arr.length - 1];

// ---------- tiny helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const slug = (s) => String(s || "").toLowerCase().normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
  .replace(/&/g, " and ").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "x";
const stateSlug = (st) => slug(STATE_NAMES[st] || st);
const fmtTime = (iso) => {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return iso || "";
  let h = Number(m[1]); const ap = h >= 12 ? "pm" : "am"; h = h % 12 || 12;
  return `${h}:${m[2]} ${ap}`;
};
const fmtDate = (iso) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
};
// A price of 0 is "not reported" on several booking platforms, never a free round.
const money = (n) => (n == null || !Number.isFinite(Number(n)) || Number(n) <= 0) ? "" : (Number(n) % 1 === 0 ? `$${Number(n)}` : `$${Number(n).toFixed(2)}`);
const priceRange = (a, b) => {
  const lo = money(a), hi = money(b);
  if (!lo) return "";
  return hi && hi !== lo ? `${lo}–${hi}` : lo;
};
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

function getJSON(url) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith("https:") ? https : http;
    const req = lib.get(url, { headers: { "User-Agent": "onetee-pages/1" } }, (res) => {
      let d = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (d += c));
      res.on("end", () => {
        if (res.statusCode !== 200) return reject(new Error(`${url} -> HTTP ${res.statusCode}: ${d.slice(0, 200)}`));
        try { resolve(JSON.parse(d)); } catch (e) { reject(new Error(`${url} -> bad JSON: ${e.message}`)); }
      });
    });
    req.on("error", reject);
    req.setTimeout(120000, () => req.destroy(new Error("timeout " + url)));
  });
}

// ---------- data ----------
async function loadDirectory() {
  if (FIXTURE) {
    const parsed = JSON.parse(fs.readFileSync(DIRECTORY_PATH, "utf8"));
    return Array.isArray(parsed) ? parsed : parsed.courses;
  }
  const j = await getJSON(`${API}/api/directory`);
  return j.courses || [];
}

async function loadTimes(states, dir) {
  const byState = {};
  if (FIXTURE) {
    // Synthetic times for a third of the online-booking courses: enough to render every template.
    for (const st of states) byState[st] = [];
    let i = 0;
    for (const c of dir) {
      if (c.booking_method !== "online" || (i++ % 3)) continue;
      const day = dayFor(c.state).date;
      for (let k = 0; k < 5; k++) {
        const hh = String(7 + k * 2).padStart(2, "0");
        byState[c.state].push({ course_slug: c.venue_id, venue_id: c.venue_id, course_name: c.name, course_label: k === 4 ? "Back 9" : "",
          city: c.city, state: c.state, teetime: `${day}T${hh}:${k * 8 < 10 ? "0" : ""}${k * 8}:00`, holes: k === 4 ? "9" : "18",
          open_spots: 1 + (k % 4), price_min: 25 + k * 5, price_max: 35 + k * 5, booking_url: c.action_url || c.website || "" });
      }
    }
    return byState;
  }
  for (const st of states) {
    const day = dayFor(st).date;
    const j = await getJSON(`${API}/api/tee-times?state=${st}&date=${day}&limit=25000`);
    byState[st] = j.tee_times || [];
  }
  return byState;
}

// What each course's sheet usually looks like (last 28 days), from /api/course-stats.
async function loadStats(states, dir) {
  const byVenue = new Map();
  if (FIXTURE) {
    let i = 0;
    for (const c of dir) if (c.booking_method === "online" && !(i++ % 3)) byVenue.set(c.venue_id, fixtureStats(c, i));
    return byVenue;
  }
  for (const st of states) {
    try {
      const j = await getJSON(`${API}/api/course-stats?state=${st}`);
      for (const s of (j.courses || [])) byVenue.set(s.venue_id, s);
    } catch (e) { console.error(`course-stats ${st}: ${e.message}`); }
  }
  return byVenue;
}
const fixtureStats = (c, i) => ({ venue_id: c.venue_id, slots: 300 + i, days: 26, price_lo: 30, price_hi: 95, price_med: 55 + (i % 3) * 5,
  price_weekday: 49, price_weekend: 69, mins_early: 420, mins_late: 990, busy_hour: 9, weekend_slots: 90, avg_spots: 2.4 });
const minsToClock = (m) => { if (m == null) return ""; let h = Math.floor(m / 60), mm = m % 60; const ap = h >= 12 ? "pm" : "am"; h = h % 12 || 12; return `${h}:${String(mm).padStart(2, "0")} ${ap}`; };
const hourToClock = (h) => { if (h == null) return ""; const ap = h >= 12 ? "pm" : "am"; return `${h % 12 || 12} ${ap}`; };
// Enough evidence to say something true: at least a week of sheets and a few dozen slots.
const statsUsable = (s) => s && s.days >= 5 && s.slots >= 30;

// Miles between two points; nearby-course lists and "towns near" links.
const milesBetween = (a, b) => {
  const R = 3958.8, toR = (x) => (x * Math.PI) / 180;
  const dLat = toR(b.lat - a.lat), dLng = toR(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toR(a.lat)) * Math.cos(toR(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
};
const hasGeo = (c) => c.lat != null && c.lng != null && Number.isFinite(Number(c.lat)) && Number.isFinite(Number(c.lng));
const fmtMiles = (m) => m < 1 ? "under a mile" : m < 10 ? `${Math.round(m * 10) / 10} mi` : `${Math.round(m)} mi`;

// ---------- page chrome ----------
const CSS = `
:root{--bg:#f4f1ea;--card:#fff;--ink:#1d1d1b;--ink2:#5b5a55;--line:#dad7ce;--olive:#5F5933;--green:#6C844C;--navy:#3b4f5c}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--navy)}a:hover{color:var(--green)}
header.top{background:var(--navy);color:#fff;padding:14px 20px}header.top .in{max-width:1080px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;gap:16px}
header.top a.brand{color:#fff;text-decoration:none;font-weight:700;font-size:20px;letter-spacing:.2px}
header.top nav a{color:#fff;text-decoration:none;margin-left:16px;font-size:14px;opacity:.92}header.top nav a.cta{background:#86b84a;color:#0f1d0a;padding:7px 12px;border-radius:999px;font-weight:700;opacity:1}
main{max-width:1080px;margin:0 auto;padding:22px 20px 60px}
.crumbs{font-size:13px;color:var(--ink2);margin:0 0 10px}.crumbs a{color:var(--ink2)}
h1{font-family:Georgia,"Times New Roman",serif;font-weight:700;font-size:32px;line-height:1.15;margin:0 0 6px}
h2{font-family:Georgia,"Times New Roman",serif;font-size:22px;margin:32px 0 10px}
.sub{color:var(--ink2);margin:0 0 18px;font-size:15px}
.note{background:#efe9d8;border:1px solid #e1d8bd;border-radius:10px;padding:12px 14px;font-size:14px;margin:0 0 22px}
.note strong{color:var(--olive)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;padding:0;margin:0;list-style:none}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile a.t{font-weight:700;text-decoration:none;font-size:17px}.tile .m{color:var(--ink2);font-size:13px;margin-top:4px}
.course{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 12px}
.course h3{margin:0 0 2px;font-family:Georgia,serif;font-size:19px}.course h3 a{text-decoration:none}
.course .meta{color:var(--ink2);font-size:13px;margin-bottom:10px}
.times{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.times li{border:1px solid var(--line);border-radius:10px;padding:7px 10px;background:#fbfaf7;font-size:14px;line-height:1.25}
.times li b{display:block;font-size:15px}.times li small{color:var(--ink2)}
.times li a{text-decoration:none;color:inherit;display:block}.times li:hover{border-color:var(--green)}
.more{font-size:13px;color:var(--ink2);margin-top:8px}
.list{columns:2;column-gap:28px;padding:0;margin:0;list-style:none;font-size:15px}.list li{break-inside:avoid;padding:3px 0}
.list li small{color:var(--ink2)}
table.tt{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
table.tt th,table.tt td{padding:9px 12px;text-align:left;border-top:1px solid var(--line);font-size:15px}table.tt th{background:#f7f5ee;border-top:0;font-size:13px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em}
.btn{display:inline-block;background:var(--ink);color:#fff;text-decoration:none;padding:9px 14px;border-radius:999px;font-weight:700;font-size:14px}
.btn.alt{background:var(--green)}.btn:hover{color:#fff;opacity:.9}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;padding:0;margin:0 0 20px;list-style:none}
.facts li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px}.facts li b{display:block;color:var(--ink2);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
figure.photo{margin:0 0 18px}figure.photo img{width:100%;max-height:420px;object-fit:cover;border-radius:14px;display:block;background:#e9e5da}figure.photo figcaption{font-size:12px;color:var(--ink2);margin-top:4px}
table.guide{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px;margin:0 0 16px}
table.guide th,table.guide td{padding:8px 10px;text-align:left;border-top:1px solid var(--line);vertical-align:top}table.guide th{background:#f7f5ee;border-top:0;font-size:12px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em}
.wrap{overflow-x:auto}
pre.snip{white-space:pre-wrap;word-break:break-all;background:#fbfaf7;border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:12px;margin:10px 0}
footer{border-top:1px solid var(--line);color:var(--ink2);font-size:13px;padding:22px 20px;text-align:center}
@media(max-width:640px){h1{font-size:26px}.list{columns:1}header.top nav a{margin-left:10px}}
`;

// Page-view beacon: path, referrer, a per-tab random id and the viewport width, to
// our own API on this host. No cookie, no IP stored, nothing personal — see
// /api/hit in vps/api/server.mjs. The /_traffic/ page is what reads it back.
const BEACON = `<script>(function(){try{if(navigator.webdriver)return;var s=sessionStorage.getItem("ot_sid");if(!s){s=Math.random().toString(36).slice(2,12);sessionStorage.setItem("ot_sid",s)}var b=JSON.stringify({p:location.pathname,r:document.referrer||"",s:s,w:innerWidth});if(!(navigator.sendBeacon&&navigator.sendBeacon("/api/hit",new Blob([b],{type:"text/plain"}))))fetch("/api/hit",{method:"POST",body:b,keepalive:true}).catch(function(){})}catch(e){}})();</script>`;

function layout({ title, desc, canonical, crumbs, body, jsonld, noindex, image, beacon = true, extraHead = "" }) {
  const crumbHtml = crumbs && crumbs.length
    ? `<p class="crumbs">${crumbs.map((c, i) => c.href ? `<a href="${esc(c.href)}">${esc(c.label)}</a>` : `<span>${esc(c.label)}</span>`).join(" › ")}</p>` : "";
  const crumbLd = crumbs && crumbs.length ? {
    "@context": "https://schema.org", "@type": "BreadcrumbList",
    itemListElement: crumbs.map((c, i) => ({ "@type": "ListItem", position: i + 1, name: c.label, ...(c.href ? { item: SITE + c.href } : {}) })),
  } : null;
  const lds = [crumbLd, ...(Array.isArray(jsonld) ? jsonld : (jsonld ? [jsonld] : []))].filter(Boolean)
    .map((o) => `<script type="application/ld+json">${JSON.stringify(o).replace(/</g, "\\u003c")}</script>`).join("\n");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
<meta name="description" content="${esc(desc)}">
<link rel="canonical" href="${esc(canonical)}">
${noindex ? `<meta name="robots" content="noindex,follow">\n` : ""}<meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(desc)}"><meta property="og:url" content="${esc(canonical)}"><meta property="og:site_name" content="OneTee"><meta property="og:image" content="${esc(image || LOGO)}">
<style>${CSS}</style>
${lds}${extraHead}
</head>
<body>
<header class="top"><div class="in"><a class="brand" href="${MAIN}/">OneTee</a><nav><a href="${SITE}/">By state</a><a href="${MAIN}/map">Map</a><a class="cta" href="${MAIN}/tee-times">Search tee times</a></nav></div></header>
<main>
${crumbHtml}
${body}
</main>
<footer>OneTee gathers public golf tee times from course booking sites. You book directly with the course; OneTee adds no fees. · <a href="${MAIN}/about">About</a> · <a href="${MAIN}/contact">Contact</a> · <a href="${MAIN}/roadmap">Roadmap</a> · <a href="/for-courses/">For golf courses</a></footer>
${beacon ? BEACON : ""}
</body>
</html>
`;
}

// ---------- model ----------
function buildModel(dir, timesByState, stats = new Map()) {
  const states = [...new Set(dir.map((c) => c.state).filter(Boolean))].sort();
  const byVenue = new Map();                 // venue_id -> { rows: [] }
  for (const st of states) {
    for (const r of (timesByState[st] || [])) {
      const vid = r.course_slug || r.venue_id;
      if (!vid) continue;
      (byVenue.get(vid) || byVenue.set(vid, { rows: [], name: r.course_name, city: r.city, state: r.state }).get(vid)).rows.push(r);
    }
  }
  for (const v of byVenue.values()) {
    v.rows.sort((a, b) => (a.teetime < b.teetime ? -1 : a.teetime > b.teetime ? 1 : 0));
    v.count = v.rows.length;
    const prices = v.rows.map((r) => r.price_min).filter((p) => p != null && Number.isFinite(Number(p)) && Number(p) > 0);
    v.fromPrice = prices.length ? Math.min(...prices.map(Number)) : null;
    v.firstBook = (v.rows.find((r) => r.booking_url) || {}).booking_url || "";
  }
  // Courses the scrapers know but the directory does not (rare) still get a page.
  const dirIds = new Set(dir.map((c) => c.venue_id));
  const extra = [];
  for (const [vid, v] of byVenue) if (!dirIds.has(vid) && v.state && STATE_NAMES[v.state]) {
    extra.push({ venue_id: vid, name: v.name || vid, city: v.city || "", state: v.state, booking_method: "online",
      label: "Book online", blurb: "", website: "", action_url: v.firstBook, phone: "", type: "", platforms: [] });
  }
  const courses = dir.concat(extra).filter((c) => c.venue_id && STATE_NAMES[c.state]);
  courses.sort((a, b) => a.name.localeCompare(b.name));
  const cityKey = (c) => `${c.state}|${slug(c.city)}`;
  const cities = new Map();                  // key -> { state, city, slug, courses: [] }
  for (const c of courses) {
    if (!c.city) continue;
    const k = cityKey(c);
    (cities.get(k) || cities.set(k, { state: c.state, city: c.city, slug: slug(c.city), courses: [] }).get(k)).courses.push(c);
  }
  // Nearest public courses to each public course (any state), for the "Nearby" section
  // and the internal links it creates. ~2,600² distances: well under a second.
  const pubGeo = courses.filter((c) => isPublic(c) && hasGeo(c));
  const nearby = new Map();                  // venue_id -> [{ c, miles }]
  for (const a of pubGeo) {
    const list = [];
    for (const b of pubGeo) {
      if (b === a) continue;
      const m = milesBetween(a, b);
      if (m <= 40) list.push({ c: b, miles: m });
    }
    list.sort((x, y) => x.miles - y.miles);
    nearby.set(a.venue_id, list.slice(0, 6));
  }
  // City centroids -> "towns near" links on city pages.
  for (const x of cities.values()) {
    const g = x.courses.filter(hasGeo);
    if (g.length) { x.lat = g.reduce((s, c) => s + Number(c.lat), 0) / g.length; x.lng = g.reduce((s, c) => s + Number(c.lng), 0) / g.length; }
  }
  const cityList = [...cities.values()].filter((x) => x.lat != null && x.courses.some(isPublic));
  for (const x of cityList) {
    x.near = cityList.filter((y) => y !== x).map((y) => ({ y, miles: milesBetween(x, y) })).filter((o) => o.miles <= 30)
      .sort((a, b) => a.miles - b.miles).slice(0, 8);
  }
  return { states: [...new Set(courses.map((c) => c.state))].sort(), courses, cities, byVenue, stats, nearby };
}

// ---------- renderers ----------
let MODEL = null; // set in main(); lets small helpers reach stats without threading it everywhere
const courseHref = (c) => `/course/${encodeURIComponent(c.venue_id)}/`;
const stateHref = (st) => `/${stateSlug(st)}/`;
const cityHref = (st, citySlug) => `/${stateSlug(st)}/${citySlug}/`;
const widgetHref = (st) => `${MAIN}/tee-times?state=${st}`;
const dealsHref = (st) => `/${stateSlug(st)}/deals/`;
const twilightHref = (st) => `/${stateSlug(st)}/twilight/`;
const cityDealsHref = (st, citySlug) => `/${stateSlug(st)}/${citySlug}/deals/`;
const TWILIGHT_MIN = 15 * 60;            // 3 pm local: where "twilight" starts on these pages
const teeMins = (iso) => { const m = /T(\d{2}):(\d{2})/.exec(iso || ""); return m ? Number(m[1]) * 60 + Number(m[2]) : null; };
// A city gets its own "cheap golf in <city>" page when there is enough to rank.
const cityDealsEligible = (model, x) => x.courses.filter((c) => isPublic(c) && statsUsable(model.stats.get(c.venue_id)) && model.stats.get(c.venue_id).price_med).length >= 4;

function timeChip(r) {
  const label = r.course_label ? ` · ${esc(r.course_label)}` : "";
  const inner = `<b>${fmtTime(r.teetime)}</b><small>${[priceRange(r.price_min, r.price_max), r.open_spots ? plural(r.open_spots, "spot") : "", r.holes ? `${esc(r.holes)} holes` : ""].filter(Boolean).join(" · ")}${label}</small>`;
  return r.booking_url ? `<li><a href="${esc(r.booking_url)}" rel="nofollow noopener" target="_blank">${inner}</a></li>` : `<li>${inner}</li>`;
}

function courseCard(c, v, { max = 8 } = {}) {
  const rows = v ? v.rows : [];
  const shown = rows.slice(0, max);
  const dw = dayFor(c.state).word;
  const s = MODEL && MODEL.stats.get(c.venue_id);
  const meta = [c.city ? `${esc(c.city)}, ${esc(c.state)}` : esc(c.state), c.type ? esc(c.type) : "",
    v ? plural(v.count, "tee time") + " " + dw : esc(c.label || ""), v && v.fromPrice != null ? `from ${money(v.fromPrice)}` : (statsUsable(s) && s.price_med ? `usually about ${money(Math.round(s.price_med))}` : "")].filter(Boolean).join(" · ");
  return `<article class="course"><h3><a href="${courseHref(c)}">${esc(c.name)}</a></h3><div class="meta">${meta}</div>` +
    (shown.length ? `<ul class="times">${shown.map(timeChip).join("")}</ul>` : "") +
    (rows.length > shown.length ? `<div class="more"><a href="${courseHref(c)}">+${rows.length - shown.length} more ${dw}</a></div>` : "") +
    (!rows.length && c.phone ? `<div class="more">${c.booking_method === "phone" ? "Call to book: " : "Phone: "}<a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a></div>` : "") +
    `</article>`;
}

function renderIndex(model, stamp) {
  const items = model.states.map((st) => {
    const cs = model.courses.filter((c) => c.state === st);
    const live = cs.filter((c) => model.byVenue.has(c.venue_id));
    const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
    const pub = cs.filter(isPublic).length;
    return `<li class="tile"><a class="t" href="${stateHref(st)}">${esc(STATE_NAMES[st])}</a><div class="m">${plural(n, "tee time")} ${dayFor(st).word} at ${plural(live.length, "course")} · ${pub} public courses</div></li>`;
  }).join("");
  const pubAll = model.courses.filter(isPublic);
  const online = pubAll.filter((c) => c.booking_method === "online").length;
  const rolled = model.states.filter((st) => dayFor(st).rolled);
  const body = `<h1>Public golf tee times today, by state</h1>
<p class="sub">Every bookable tee time we can find at public courses, in one place, updated through the day. Pick a state, or <a href="${MAIN}/tee-times">search with filters</a> for time, price, holes and distance.</p>
<ul class="grid">${items}</ul>
<p class="sub" style="margin-top:14px">Cheapest golf: ${model.states.map((st) => `<a href="${dealsHref(st)}">${esc(STATE_NAMES[st])}</a>`).join(" · ")}</p>
<p class="sub">Twilight tee times: ${model.states.map((st) => `<a href="${twilightHref(st)}">${esc(STATE_NAMES[st])}</a>`).join(" · ")}</p>
<h2>How this works</h2>
<p>OneTee lists ${pubAll.length.toLocaleString("en-US")} public, municipal, resort and semi-private golf courses across ${model.states.length} states. ${online.toLocaleString("en-US")} of them sell tee times online through their own booking systems — ForeUp, TeeItUp, Chronogolf, GolfNow, EZLinks, Club Prophet and others — and OneTee reads those sheets through the day and puts every open slot on one page. Each time links straight to the course's own booking page, so you pay the course's price with no fees or markup added. Courses that only take bookings by phone are listed with their pro-shop number.</p>
<p>Pages refresh every two hours. After 6 pm local time a state's page rolls over to tomorrow's tee times, since the day's sheet is mostly played out by then${rolled.length ? ` (${listNames(rolled.map((st) => STATE_NAMES[st]))} ${rolled.length === 1 ? "is" : "are"} showing tomorrow right now)` : ""}.</p>
<p class="note"><strong>Planning ahead?</strong> Today's times are free to browse. A free OneTee account adds filters and saved searches; Premium opens the next 30 days for $3 a month. <a href="${MAIN}/tee-times">Start on OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}.</p>`;
  return layout({ title: `Golf tee times today across ${model.states.length} states — OneTee`, desc: `Browse today's public golf tee times by state: ${pubAll.length.toLocaleString("en-US")} public courses, every open slot, book direct with the course. No fees, no markup.`,
    canonical: SITE + "/", crumbs: [], body,
    jsonld: { "@context": "https://schema.org", "@type": "WebSite", name: "OneTee tee times", url: SITE + "/", publisher: ORG_LD } });
}

function renderState(model, st, stamp) {
  const name = STATE_NAMES[st];
  const D = dayFor(st);
  const cs = model.courses.filter((c) => c.state === st);
  const pub = cs.filter(isPublic), priv = cs.filter((c) => !isPublic(c));
  const live = cs.filter((c) => model.byVenue.has(c.venue_id));
  const rest = cs.filter((c) => !model.byVenue.has(c.venue_id));
  const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
  const cities = [...model.cities.values()].filter((x) => x.state === st).sort((a, b) => a.city.localeCompare(b.city));
  const cityList = cities.map((x) => {
    const k = x.courses.filter((c) => model.byVenue.has(c.venue_id)).reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
    return `<li><a href="${cityHref(st, x.slug)}">${esc(x.city)}</a> <small>${x.courses.length} ${x.courses.length === 1 ? "course" : "courses"}${k ? ` · ${k} ${D.word}` : ""}</small></li>`;
  }).join("");
  // Evergreen intro: the facts about golf in this state that do not change hour to hour.
  const online = pub.filter((c) => c.booking_method === "online"), byPhone = pub.filter((c) => c.booking_method === "phone");
  const topCities = cities.map((x) => ({ x, k: x.courses.filter(isPublic).length })).filter((o) => o.k >= 2).sort((a, b) => b.k - a.k).slice(0, 5);
  const platCount = {};
  for (const c of online) for (const p of (c.platforms || [])) if (PLATFORM_NAMES[p]) platCount[PLATFORM_NAMES[p]] = (platCount[PLATFORM_NAMES[p]] || 0) + 1;
  const plats = Object.entries(platCount).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k]) => k);
  const typeCount = {};
  for (const c of pub) if (c.type) typeCount[c.type] = (typeCount[c.type] || 0) + 1;
  const types = Object.entries(typeCount).sort((a, b) => b[1] - a[1]).map(([t, k]) => `${k} ${t.toLowerCase()}`);
  const intro = `<h2>Public golf in ${esc(name)}</h2>
<p>OneTee lists ${pub.length} golf courses in ${esc(name)} that sell rounds to the public${types.length ? ` — ${esc(listNames(types))}` : ""}${priv.length ? ` — plus ${priv.length} private and military clubs for reference` : ""}. ${topCities.length ? `The biggest golf towns are ${topCities.map((o) => `<a href="${cityHref(st, o.x.slug)}">${esc(o.x.city)}</a> (${o.k})`).join(", ")}; ` : ""}${cities.length} cities and towns in all.</p>
<p>${online.length} of these courses take bookings online${plats.length ? `, mostly through ${esc(listNames(plats))}` : ""}, and OneTee reads their tee sheets through the day so every open slot shows here with the course's own price. ${byPhone.length ? `${byPhone.length} courses only book by phone; their pro-shop numbers are listed. ` : ""}Booking always happens with the course itself — OneTee adds no fees.</p>`;
  const body = `<h1>Golf tee times ${D.word} in ${esc(name)}</h1>
<p class="sub">${D.Word}, ${fmtDate(D.date)} · ${plural(n, "open tee time")} at ${plural(live.length, "public course")} · ${pub.length} public courses listed. <a href="${dealsHref(st)}">Cheapest golf in ${esc(name)}</a> · <a href="${twilightHref(st)}">Twilight tee times</a> · <a href="${widgetHref(st)}">Filter on OneTee →</a></p>
${D.rolled ? `<p class="note">It's evening in ${esc(name)}, so this page has rolled over to <strong>tomorrow's</strong> tee times. The day's remaining slots are on <a href="${widgetHref(st)}">the live search</a>.</p>` : ""}
${live.length ? `<h2>Courses with tee times ${D.word}</h2>${live.map((c) => courseCard(c, model.byVenue.get(c.venue_id))).join("")}` : `<p class="note">No open tee times are listed for ${D.word} right now. Courses often release or free up slots through the day — <a href="${widgetHref(st)}">check the live search</a>, or call a course below.</p>`}
${intro}
${cities.length ? `<h2>By city</h2><ul class="list">${cityList}</ul>` : ""}
${rest.length ? `<h2>More ${esc(name)} courses</h2><ul class="list">${rest.map((c) => `<li><a href="${courseHref(c)}">${esc(c.name)}</a> <small>${esc(c.city || "")}${c.label ? ` · ${esc(c.label)}` : ""}</small></li>`).join("")}</ul>` : ""}
<p class="note"><strong>Want the weekend, or next week?</strong> A free account adds filters and saved searches; Premium members see the next 30 days. <a href="${widgetHref(st)}">Open OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `${name} golf tee times today — ${pub.length} public courses — OneTee`,
    desc: `Today's open tee times at public golf courses across ${name}: ${pub.length} courses in ${cities.length} cities and towns, with prices, booking links and phone numbers. Book direct, no fees.`,
    canonical: SITE + stateHref(st), crumbs: [{ label: "Tee times", href: "/" }, { label: name }], body,
    jsonld: { "@context": "https://schema.org", "@type": "ItemList", name: `Golf courses in ${name}`, numberOfItems: cs.length,
      itemListElement: cs.slice(0, 200).map((c, i) => ({ "@type": "ListItem", position: i + 1, name: c.name, url: SITE + courseHref(c) })) } });
}

function renderCity(model, x, stamp) {
  const st = x.state, name = STATE_NAMES[st];
  const D = dayFor(st);
  const live = x.courses.filter((c) => model.byVenue.has(c.venue_id));
  const rest = x.courses.filter((c) => !model.byVenue.has(c.venue_id));
  const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
  const pub = x.courses.filter(isPublic), priv = x.courses.filter((c) => !isPublic(c));
  const online = pub.filter((c) => c.booking_method === "online").length;
  const intro = pub.length ? `<p>${esc(x.city)} has ${pub.length === 1 ? "one public golf course" : `${pub.length} golf courses open to the public`} on OneTee: ${listNames(pub.map((c) => `<a href="${courseHref(c)}">${esc(c.name)}</a>${c.type ? ` (${esc(c.type.toLowerCase())})` : ""}`))}.${online ? ` ${online === pub.length ? (pub.length === 1 ? "It takes" : "All of them take") : `${online} of them take`} bookings online, and the open slots show here with the course's own price.` : ""}${priv.length ? ` ${listNames(priv.map((c) => esc(c.name)))} ${priv.length === 1 ? "is a private club" : "are private clubs"} — no public tee times.` : ""}</p>` : "";
  // The guide: for towns with two or more public courses, the page a golfer new to
  // town would want — every course with type, how it books, what it usually costs
  // and when its sheet runs, then how booking works here and the towns next door.
  let guide = "";
  if (pub.length >= 2) {
    const withStats = pub.map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.price_med);
    const cheapest = withStats.length ? withStats.reduce((a, b) => (a.s.price_med <= b.s.price_med ? a : b)) : null;
    const dearest = withStats.length > 1 ? withStats.reduce((a, b) => (a.s.price_med >= b.s.price_med ? a : b)) : null;
    const earliest = pub.map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.mins_early != null).sort((a, b) => a.s.mins_early - b.s.mins_early)[0];
    const typeCount = {};
    for (const c of pub) if (c.type) typeCount[c.type] = (typeCount[c.type] || 0) + 1;
    const types = Object.entries(typeCount).sort((a, b) => b[1] - a[1]).map(([t, k]) => `${k} ${t.toLowerCase()}`);
    const platCount = {};
    for (const c of pub) for (const p of (c.platforms || [])) if (PLATFORM_NAMES[p]) platCount[PLATFORM_NAMES[p]] = (platCount[PLATFORM_NAMES[p]] || 0) + 1;
    const plats = Object.entries(platCount).sort((a, b) => b[1] - a[1]).map(([k]) => k);
    const phoneOnly = pub.filter((c) => c.booking_method === "phone");
    const rowsHtml = pub.map((c) => { const s = model.stats.get(c.venue_id); const ok = statsUsable(s);
      return `<tr><td><a href="${courseHref(c)}">${esc(c.name)}</a></td><td>${esc(c.type || "")}</td><td>${esc(c.label || "")}</td><td>${ok && s.price_med ? money(Math.round(s.price_med)) + (s.price_weekend && s.price_weekday && Math.round(s.price_weekend) !== Math.round(s.price_weekday) ? `<br><small>${money(Math.round(s.price_weekday))} wk / ${money(Math.round(s.price_weekend))} wknd</small>` : "") : ""}</td><td>${ok && s.mins_early != null ? `${minsToClock(s.mins_early)} – ${minsToClock(s.mins_late)}` : ""}</td><td>${c.phone ? `<a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a>` : ""}</td></tr>`; }).join("");
    const nearTowns = (x.near || []).filter((o) => o.y.courses.filter(isPublic).length);
    guide = `<h2 id="guide">Guide to public golf in ${esc(x.city)}</h2>
<p>${esc(x.city)} has ${pub.length} courses that sell tee times to the public${types.length > 1 ? ` (${esc(listNames(types))})` : ""}. ${cheapest ? `The best value by typical price is <a href="${courseHref(cheapest.c)}">${esc(cheapest.c.name)}</a> at about ${money(Math.round(cheapest.s.price_med))}${dearest && dearest !== cheapest ? `; <a href="${courseHref(dearest.c)}">${esc(dearest.c.name)}</a> is the priciest at about ${money(Math.round(dearest.s.price_med))}` : ""}. ` : ""}${earliest ? `For an early start, <a href="${courseHref(earliest.c)}">${esc(earliest.c.name)}</a> has tee times from around ${minsToClock(earliest.s.mins_early)}. ` : ""}${withStats.length ? `Typical prices come from the last 28 days of each course's tee sheet; weekday and weekend rates are shown where they differ.` : ""}</p>
<div class="wrap"><table class="guide"><thead><tr><th>Course</th><th>Type</th><th>Booking</th><th>Typical price</th><th>Tee times run</th><th>Phone</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>
<h3>How to book a tee time in ${esc(x.city)}</h3>
<p>${online ? `${online === pub.length ? "Every course here" : `${online} of the ${pub.length} courses`} take${online === 1 ? "s" : ""} bookings online through ${plats.length ? esc(listNames(plats.slice(0, 4))) : "the course's own tee sheet"}, and those open slots appear on this page with a Book link that goes straight to the course — you pay the course's price, and OneTee adds nothing. ` : ""}${phoneOnly.length ? `${listNames(phoneOnly.map((c) => `<a href="${courseHref(c)}">${esc(c.name)}</a>`))} ${phoneOnly.length === 1 ? "books" : "book"} by phone only; call the pro shop. ` : ""}Today's times are free to browse; a free OneTee account adds filters and saved searches, and Premium opens the next 30 days.</p>
${cityDealsEligible(model, x) ? `<p class="note"><strong>On a budget?</strong> <a href="${cityDealsHref(st, x.slug)}">Cheap golf in ${esc(x.city)}</a> ranks every public course here by typical price, with today's lowest open rates.</p>` : ""}
${nearTowns.length ? `<h3>Golf towns near ${esc(x.city)}</h3><ul class="list">${nearTowns.map((o) => `<li><a href="${cityHref(o.y.state, o.y.slug)}">${esc(o.y.city)}${o.y.state !== st ? `, ${esc(o.y.state)}` : ""}</a> <small>${fmtMiles(o.miles)} · ${plural(o.y.courses.filter(isPublic).length, "public course")}</small></li>`).join("")}</ul>` : ""}`;
  }
  const body = `<h1>Golf tee times in ${esc(x.city)}, ${esc(st)}</h1>
<p class="sub">${D.Word}, ${fmtDate(D.date)} · ${plural(n, "open tee time")} at ${plural(live.length, "course")} in ${esc(x.city)} · <a href="${stateHref(st)}">all of ${esc(name)}</a> · <a href="${widgetHref(st)}">search nearby on OneTee →</a></p>
${intro}
${live.map((c) => courseCard(c, model.byVenue.get(c.venue_id))).join("")}
${rest.length ? `<h2>${live.length ? "Other courses" : "Courses"} in ${esc(x.city)}</h2>${rest.map((c) => courseCard(c, null)).join("")}` : ""}
${guide}
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: pub.length >= 2 ? `${x.city}, ${st} golf tee times today — ${pub.length} public courses — OneTee` : `${x.city}, ${st} golf tee times today — OneTee`,
    desc: pub.length >= 2 ? `Open tee times today at ${pub.length} public golf courses in ${x.city}, ${name}, plus a guide: typical prices, when each sheet runs, how to book, and towns nearby. Book direct, no fees.`
      : `Open tee times today at the public golf course in ${x.city}, ${name}, with prices and booking links. Book direct with the course, no fees.`,
    canonical: SITE + cityHref(st, x.slug), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, { label: x.city }], body,
    noindex: !cityIndexable(x) });
}

function renderCourse(model, c, stamp) {
  const st = c.state, name = STATE_NAMES[st];
  const v = model.byVenue.get(c.venue_id);
  const rows = v ? v.rows : [];
  const D = dayFor(st);
  const pub = isPublic(c);
  const cityLink = c.city ? `<a href="${cityHref(st, slug(c.city))}">${esc(c.city)}</a>, ` : "";
  const facts = [
    c.phone ? `<li><b>Phone</b><a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a></li>` : "",
    c.website ? `<li><b>Website</b><a href="${esc(c.website)}" rel="nofollow noopener" target="_blank">${esc(c.website.replace(/^https?:\/\//, "").replace(/\/$/, ""))}</a></li>` : "",
    c.type ? `<li><b>Course</b>${esc(c.type)}</li>` : "",
    c.label ? `<li><b>Booking</b>${esc(c.label)}</li>` : "",
  ].filter(Boolean).join("");
  const table = rows.length ? `<table class="tt"><thead><tr><th>Time</th><th>Course</th><th>Holes</th><th>Spots</th><th>Price</th><th></th></tr></thead><tbody>${rows.map((r) =>
    `<tr><td><b>${fmtTime(r.teetime)}</b></td><td>${esc(r.course_label || "")}</td><td>${esc(r.holes || "")}</td><td>${r.open_spots ?? ""}</td><td>${priceRange(r.price_min, r.price_max)}</td><td>${r.booking_url ? `<a href="${esc(r.booking_url)}" rel="nofollow noopener" target="_blank">Book</a>` : ""}</td></tr>`).join("")}</tbody></table>` : "";
  const bookBtn = (rows.length && v.firstBook) ? v.firstBook : (c.action_url || "");
  // Photo from the course's own site (data/course_photos.json -> directory.photo). Credited and linked.
  const photo = c.photo && /^https?:\/\//.test(c.photo) ? `<figure class="photo"><img src="${esc(c.photo)}" alt="${esc(c.name)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.remove()"><figcaption>Photo: <a href="${esc(c.website || c.photo)}" rel="nofollow noopener" target="_blank">${esc((c.website || c.photo).replace(/^https?:\/\/(www\.)?/, "").split("/")[0])}</a></figcaption></figure>` : "";
  // What to expect: the last 28 days of this course's sheet, in plain words.
  const s = model.stats.get(c.venue_id);
  let expect = "";
  if (pub && statsUsable(s)) {
    const perDay = Math.round(s.slots / s.days);
    const wkShare = s.slots ? Math.round((100 * s.weekend_slots) / s.slots) : 0;
    const price = s.price_med ? `<li><b>Typical price</b>${money(Math.round(s.price_med))}${s.price_lo && s.price_hi && s.price_hi > s.price_lo ? ` <small>(seen from ${money(Math.round(s.price_lo))} to ${money(Math.round(s.price_hi))})</small>` : ""}</li>` : "";
    const wk = s.price_weekday && s.price_weekend && Math.round(s.price_weekday) !== Math.round(s.price_weekend)
      ? `<li><b>Weekday / weekend</b>${money(Math.round(s.price_weekday))} / ${money(Math.round(s.price_weekend))}</li>` : "";
    const hours = s.mins_early != null && s.mins_late != null ? `<li><b>Tee times run</b>${minsToClock(s.mins_early)} – ${minsToClock(s.mins_late)}</li>` : "";
    const busy = s.busy_hour != null ? `<li><b>Open slots cluster at</b>${hourToClock(s.busy_hour)}</li>` : "";
    const vol = `<li><b>Open slots per day</b>about ${perDay}</li>`;
    const grp = s.avg_spots ? `<li><b>Typical opening</b>${s.avg_spots >= 3.5 ? "room for a foursome" : s.avg_spots >= 2.5 ? "2–3 spots" : "1–2 spots"}</li>` : "";
    expect = `<h2>What to expect at ${esc(c.name)}</h2>
<p>Over the last ${s.days} days we saw ${s.slots.toLocaleString("en-US")} open tee times on ${esc(c.name)}'s sheet — about ${perDay} a day${s.price_med ? `, with a typical rate of ${money(Math.round(s.price_med))}` : ""}${s.price_weekday && s.price_weekend && Math.round(s.price_weekend) > Math.round(s.price_weekday) ? ` (weekends run about ${money(Math.round(s.price_weekend))} against ${money(Math.round(s.price_weekday))} on weekdays)` : ""}. ${s.mins_early != null ? `Most tee times fall between ${minsToClock(s.mins_early)} and ${minsToClock(s.mins_late)}${s.busy_hour != null ? `, and the most open slots we see are around ${hourToClock(s.busy_hour)}` : ""}. ` : ""}${wkShare ? `Weekend days account for ${wkShare}% of the open slots we saw${wkShare < 22 ? " — weekends fill first, so book those early" : ""}.` : ""}</p>
<ul class="facts">${price}${wk}${hours}${busy}${vol}${grp}</ul>`;
  }
  // Nearby public courses, with distance — real internal links, useful to a golfer.
  const near = pub ? (model.nearby.get(c.venue_id) || []) : [];
  const nearHtml = near.length ? `<h2>Public courses near ${esc(c.name)}</h2>
<ul class="list">${near.map((o) => { const ns = model.stats.get(o.c.venue_id); return `<li><a href="${courseHref(o.c)}">${esc(o.c.name)}</a> <small>${fmtMiles(o.miles)}${o.c.city ? ` · ${esc(o.c.city)}` : ""}${o.c.type ? ` · ${esc(o.c.type)}` : ""}${statsUsable(ns) && ns.price_med ? ` · about ${money(Math.round(ns.price_med))}` : ""}</small></li>`; }).join("")}</ul>` : "";
  const sheet = pub ? `<h2>${D.Word}, ${fmtDate(D.date)}</h2>
${rows.length ? `<p class="sub">${plural(rows.length, "open tee time")}${v.fromPrice != null ? ` from ${money(v.fromPrice)}` : ""}. Times as of ${esc(stamp)}; book directly with the course.</p>${table}` :
    `<p class="note">No open tee times are listed for ${D.word}${c.booking_method === "online" ? " right now — the course may release more through the day" : ""}. ${c.booking_method === "phone" && c.phone ? `This course takes bookings by phone: <a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a>.` : ""} ${c.action_url ? `<a href="${esc(c.action_url)}" rel="nofollow noopener" target="_blank">Check the course's booking page →</a>` : ""}</p>`}`
    : `<p class="note">${esc(c.name)} is a ${c.booking_method === "military" ? "military" : "private"} club: ${esc(c.blurb || "no public tee times.")} For courses near ${esc(c.city || name)} that sell rounds to the public, see <a href="${c.city ? cityHref(st, slug(c.city)) : stateHref(st)}">${esc(c.city || name)}</a>.</p>`;
  const body = `<h1>${esc(c.name)}${pub ? " tee times" : ""}</h1>
<p class="sub">${cityLink}<a href="${stateHref(st)}">${esc(name)}</a>${c.blurb && pub ? ` · ${esc(c.blurb)}` : ""}</p>
${photo}
<ul class="facts">${facts}</ul>
${sheet}
<p style="margin:18px 0">${bookBtn && pub ? `<a class="btn" href="${esc(bookBtn)}" rel="nofollow noopener" target="_blank">Book at ${esc(c.name)}</a> ` : ""}<a class="btn alt" href="${widgetHref(st)}">See nearby courses on OneTee</a></p>
${expect}
${nearHtml}
${pub && courseIndexable(c) ? operatorBox(c) : ""}
${pub ? `<p class="note"><strong>Tomorrow and beyond:</strong> Premium members see the next 30 days at every course; a free account adds filters and saved searches. <a href="${MAIN}/tee-times">Open OneTee →</a></p>` : ""}`;
  const ld = { "@context": "https://schema.org", "@type": "GolfCourse", name: c.name, url: SITE + courseHref(c),
    ...(c.phone ? { telephone: c.phone } : {}), ...(c.website ? { sameAs: c.website } : {}),
    ...(photo ? { image: c.photo } : {}),
    ...(pub && statsUsable(s) && s.price_lo && s.price_hi ? { priceRange: `${money(Math.round(s.price_lo))}–${money(Math.round(s.price_hi))}` } : {}),
    ...(c.booking_method === "private" ? { publicAccess: false } : pub ? { publicAccess: true } : {}),
    address: { "@type": "PostalAddress", ...(c.city ? { addressLocality: c.city } : {}), addressRegion: st, ...(c.zip ? { postalCode: c.zip } : {}), addressCountry: "US" },
    ...(c.lat != null && c.lng != null ? { geo: { "@type": "GeoCoordinates", latitude: c.lat, longitude: c.lng } } : {}) };
  const where = `${c.city ? c.city + ", " : ""}${st}`;
  return layout({ title: pub ? `${c.name} tee times — ${where} — OneTee` : `${c.name} — ${where} — OneTee`,
    desc: pub ? `${c.name} in ${c.city || name}: today's open tee times${statsUsable(s) && s.price_med ? ` (usually about ${money(Math.round(s.price_med))})` : ""}, what the sheet normally looks like, booking link${c.phone ? ", phone" : ""}, website and nearby courses. Book direct; no fees.`
      : `${c.name} in ${c.city || name} is a ${c.booking_method === "military" ? "military" : "private"} club. Find public courses nearby on OneTee.`,
    canonical: SITE + courseHref(c), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, ...(c.city ? [{ label: c.city, href: cityHref(st, slug(c.city)) }] : []), { label: c.name }],
    body, jsonld: ld, noindex: !courseIndexable(c), image: photo ? c.photo : undefined });
}


// ---------- deals, twilight, for-courses, traffic ----------
// Live rows for a state, one per (course, time), cheapest first.
function liveRows(model, st, filter = () => true) {
  const out = [];
  for (const c of model.courses) {
    if (c.state !== st || !isPublic(c)) continue;
    const v = model.byVenue.get(c.venue_id);
    if (!v) continue;
    for (const r of v.rows) if (filter(r)) out.push({ c, r });
  }
  return out;
}

function dealRow({ c, r }) {
  return `<tr><td><a href="${courseHref(c)}">${esc(c.name)}</a><br><small>${esc(c.city || "")}</small></td><td><b>${fmtTime(r.teetime)}</b></td><td>${priceRange(r.price_min, r.price_max)}</td><td>${r.open_spots ?? ""}</td><td>${esc(r.holes || "")}</td><td>${r.booking_url ? `<a href="${esc(r.booking_url)}" rel="nofollow noopener" target="_blank">Book</a>` : ""}</td></tr>`;
}

function renderDeals(model, st, stamp) {
  const name = STATE_NAMES[st], D = dayFor(st);
  const pub = model.courses.filter((c) => c.state === st && isPublic(c));
  const withStats = pub.map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.price_med).sort((a, b) => a.s.price_med - b.s.price_med);
  const cheapLive = liveRows(model, st, (r) => Number(r.price_min) > 0).sort((a, b) => Number(a.r.price_min) - Number(b.r.price_min));
  const seen = new Set(), top = [];
  for (const o of cheapLive) { if (seen.has(o.c.venue_id)) continue; seen.add(o.c.venue_id); top.push(o); if (top.length >= 25) break; }
  const under = (n) => withStats.filter((o) => o.s.price_med <= n).length;
  const median = withStats.length ? withStats[Math.floor(withStats.length / 2)].s.price_med : null;
  const body = `<h1>Cheapest public golf in ${esc(name)}</h1>
<p class="sub">${withStats.length} public courses ranked by what a tee time usually costs, plus ${D.word}'s lowest open rates. <a href="${stateHref(st)}">All ${esc(name)} tee times</a> · <a href="${twilightHref(st)}">Twilight tee times</a></p>
${top.length ? `<h2>Lowest open rates ${D.word}, ${fmtDate(D.date)}</h2>
<p class="sub">The cheapest open slot at each course right now — one row per course, ${top.length} courses. As of ${esc(stamp)}.</p>
<div class="wrap"><table class="tt"><thead><tr><th>Course</th><th>Time</th><th>Price</th><th>Spots</th><th>Holes</th><th></th></tr></thead><tbody>${top.map(dealRow).join("")}</tbody></table></div>` : `<p class="note">No priced open tee times are listed for ${D.word} right now — check back after the next refresh, or <a href="${widgetHref(st)}">search live on OneTee</a>.</p>`}
<h2>Best value courses in ${esc(name)}</h2>
<p>${withStats.length ? `Across the last 28 days of tee sheets, the typical tee time in ${esc(name)} runs about ${money(Math.round(median))}. ${under(30) ? `${plural(under(30), "course")} usually come in under $30` : ""}${under(30) && under(50) > under(30) ? `, ${under(50)} under $50` : under(50) ? `${plural(under(50), "course")} usually come in under $50` : ""}. Prices below are the median first-listed rate at each course; weekend rates are shown where they differ. Municipal and 9-hole courses lead the list, as you'd expect — a cheap round is still a real round.` : `We don't have enough recent sheet data to rank ${esc(name)} courses by price yet; check the state page for today's open rates.`}</p>
${withStats.length ? `<div class="wrap"><table class="guide"><thead><tr><th>#</th><th>Course</th><th>Town</th><th>Type</th><th>Typical price</th><th>Weekday / weekend</th><th>Tee times run</th></tr></thead><tbody>${withStats.slice(0, 60).map((o, i) => `<tr><td>${i + 1}</td><td><a href="${courseHref(o.c)}">${esc(o.c.name)}</a></td><td>${o.c.city ? `<a href="${cityHref(st, slug(o.c.city))}">${esc(o.c.city)}</a>` : ""}</td><td>${esc(o.c.type || "")}</td><td><b>${money(Math.round(o.s.price_med))}</b></td><td>${o.s.price_weekday && o.s.price_weekend ? `${money(Math.round(o.s.price_weekday))} / ${money(Math.round(o.s.price_weekend))}` : ""}</td><td>${o.s.mins_early != null ? `${minsToClock(o.s.mins_early)} – ${minsToClock(o.s.mins_late)}` : ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
<h2>How to pay less for golf in ${esc(name)}</h2>
<p>Twilight rates are the reliable one: most courses drop the price for the last few hours of light, and the <a href="${twilightHref(st)}">twilight page</a> lists ${D.word}'s late slots. Weekday mornings after the early rush are next. Nine-hole and executive courses cost a fraction of a championship layout and are listed above with the rest. And booking direct — which every link on OneTee does — means you pay the course's own rate with nothing added on top. A free OneTee account adds a price filter and saved searches; Premium shows the next 30 days so you can pick the cheap day, not just the cheap hour.</p>
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `Cheapest public golf in ${name} — courses ranked by price — OneTee`,
    desc: `${name}'s public golf courses ranked by typical tee-time price from the last 28 days of tee sheets, plus today's lowest open rates. Book direct, no fees.`,
    canonical: SITE + dealsHref(st), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, { label: "Cheapest golf" }], body,
    jsonld: withStats.length ? { "@context": "https://schema.org", "@type": "ItemList", name: `Cheapest public golf courses in ${name}`, itemListOrder: "https://schema.org/ItemListOrderAscending",
      itemListElement: withStats.slice(0, 20).map((o, i) => ({ "@type": "ListItem", position: i + 1, name: o.c.name, url: SITE + courseHref(o.c) })) } : null });
}

function renderCityDeals(model, x, stamp) {
  const st = x.state, name = STATE_NAMES[st], D = dayFor(st);
  const pub = x.courses.filter(isPublic);
  const withStats = pub.map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.price_med).sort((a, b) => a.s.price_med - b.s.price_med);
  const ids = new Set(pub.map((c) => c.venue_id));
  // Nearby public courses (within 25 mi of any course in town) widen a small town's list honestly.
  const nearIds = new Set();
  for (const c of pub) for (const o of (model.nearby.get(c.venue_id) || [])) if (o.miles <= 25 && !ids.has(o.c.venue_id)) nearIds.add(o.c.venue_id);
  const nearStats = [...nearIds].map((id) => model.courses.find((c) => c.venue_id === id)).filter(Boolean).map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.price_med).sort((a, b) => a.s.price_med - b.s.price_med).slice(0, 10);
  const cheapLive = liveRows(model, st, (r) => Number(r.price_min) > 0).filter((o) => ids.has(o.c.venue_id)).sort((a, b) => Number(a.r.price_min) - Number(b.r.price_min));
  const seen = new Set(), top = [];
  for (const o of cheapLive) { if (seen.has(o.c.venue_id)) continue; seen.add(o.c.venue_id); top.push(o); if (top.length >= 15) break; }
  const row = (o, i) => `<tr><td>${i + 1}</td><td><a href="${courseHref(o.c)}">${esc(o.c.name)}</a>${o.c.city && o.c.city !== x.city ? `<br><small>${esc(o.c.city)}</small>` : ""}</td><td>${esc(o.c.type || "")}</td><td><b>${money(Math.round(o.s.price_med))}</b></td><td>${o.s.price_weekday && o.s.price_weekend ? `${money(Math.round(o.s.price_weekday))} / ${money(Math.round(o.s.price_weekend))}` : ""}</td><td>${o.s.mins_early != null ? `${minsToClock(o.s.mins_early)} – ${minsToClock(o.s.mins_late)}` : ""}</td></tr>`;
  const body = `<h1>Cheap golf in ${esc(x.city)}, ${esc(st)}</h1>
<p class="sub">${withStats.length} public courses in ${esc(x.city)} ranked by typical tee-time price, plus ${D.word}'s lowest open rates. <a href="${cityHref(st, x.slug)}">All ${esc(x.city)} tee times</a> · <a href="${dealsHref(st)}">Cheapest golf in ${esc(name)}</a></p>
${top.length ? `<h2>Lowest open rates ${D.word}, ${fmtDate(D.date)}</h2><div class="wrap"><table class="tt"><thead><tr><th>Course</th><th>Time</th><th>Price</th><th>Spots</th><th>Holes</th><th></th></tr></thead><tbody>${top.map(dealRow).join("")}</tbody></table></div>` : `<p class="note">No priced open tee times in ${esc(x.city)} are listed for ${D.word} right now. <a href="${widgetHref(st)}">Search live on OneTee</a>.</p>`}
<h2>${esc(x.city)} public courses by typical price</h2>
<p>Ranked by the median first-listed rate over the last 28 days of each course's tee sheet. <a href="${courseHref(withStats[0].c)}">${esc(withStats[0].c.name)}</a> is the cheapest regular round at about ${money(Math.round(withStats[0].s.price_med))}; <a href="${courseHref(withStats[withStats.length - 1].c)}">${esc(withStats[withStats.length - 1].c.name)}</a> tops the list at about ${money(Math.round(withStats[withStats.length - 1].s.price_med))}. Weekday and weekend medians are shown where they differ.</p>
<div class="wrap"><table class="guide"><thead><tr><th>#</th><th>Course</th><th>Type</th><th>Typical price</th><th>Weekday / weekend</th><th>Tee times run</th></tr></thead><tbody>${withStats.map(row).join("")}</tbody></table></div>
${nearStats.length ? `<h2>Cheap rounds within 25 miles of ${esc(x.city)}</h2><div class="wrap"><table class="guide"><thead><tr><th>#</th><th>Course</th><th>Type</th><th>Typical price</th><th>Weekday / weekend</th><th>Tee times run</th></tr></thead><tbody>${nearStats.map(row).join("")}</tbody></table></div>` : ""}
<p class="note"><strong>Want the cheap day, not just the cheap hour?</strong> A free OneTee account adds a price filter; Premium shows the next 30 days at every course. <a href="${widgetHref(st)}">Open OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `Cheap golf in ${x.city}, ${st} — public courses ranked by price — OneTee`,
    desc: `${x.city}'s public golf courses ranked by typical tee-time price, with today's lowest open rates and cheap rounds nearby. Book direct with the course, no fees.`,
    canonical: SITE + cityDealsHref(st, x.slug), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, { label: x.city, href: cityHref(st, x.slug) }, { label: "Cheap golf" }], body,
    jsonld: { "@context": "https://schema.org", "@type": "ItemList", name: `Cheapest public golf courses in ${x.city}, ${st}`, itemListOrder: "https://schema.org/ItemListOrderAscending",
      itemListElement: withStats.slice(0, 20).map((o, i) => ({ "@type": "ListItem", position: i + 1, name: o.c.name, url: SITE + courseHref(o.c) })) } });
}

function renderTwilight(model, st, stamp) {
  const name = STATE_NAMES[st], D = dayFor(st);
  const rows = liveRows(model, st, (r) => { const m = teeMins(r.teetime); return m != null && m >= TWILIGHT_MIN; });
  const byCourse = new Map();
  for (const o of rows) (byCourse.get(o.c.venue_id) || byCourse.set(o.c.venue_id, { c: o.c, rows: [] }).get(o.c.venue_id)).rows.push(o.r);
  const groups = [...byCourse.values()].map((g) => { g.rows.sort((a, b) => (a.teetime < b.teetime ? -1 : 1)); const p = g.rows.map((r) => Number(r.price_min)).filter((n) => n > 0); g.from = p.length ? Math.min(...p) : null; return g; })
    .sort((a, b) => (a.from ?? 1e9) - (b.from ?? 1e9));
  const late = model.courses.filter((c) => c.state === st && isPublic(c)).map((c) => ({ c, s: model.stats.get(c.venue_id) })).filter((o) => statsUsable(o.s) && o.s.mins_late != null && o.s.mins_late >= 17 * 60).sort((a, b) => b.s.mins_late - a.s.mins_late).slice(0, 30);
  const body = `<h1>Twilight tee times in ${esc(name)} ${D.word}</h1>
<p class="sub">${D.Word}, ${fmtDate(D.date)} · ${plural(rows.length, "open tee time")} from 3 pm at ${plural(groups.length, "course")}, cheapest course first. <a href="${stateHref(st)}">All ${esc(name)} tee times</a> · <a href="${dealsHref(st)}">Cheapest golf in ${esc(name)}</a></p>
${groups.length ? groups.map((g) => `<article class="course"><h3><a href="${courseHref(g.c)}">${esc(g.c.name)}</a></h3><div class="meta">${esc(g.c.city || "")}${g.c.type ? ` · ${esc(g.c.type)}` : ""} · ${plural(g.rows.length, "late tee time")}${g.from ? ` · from ${money(g.from)}` : ""}</div><ul class="times">${g.rows.slice(0, 10).map(timeChip).join("")}</ul>${g.rows.length > 10 ? `<div class="more"><a href="${courseHref(g.c)}">+${g.rows.length - 10} more</a></div>` : ""}</article>`).join("") : `<p class="note">No open tee times from 3 pm are listed for ${D.word} right now. Afternoon slots often open up as the day goes on — <a href="${widgetHref(st)}">check the live search</a>.</p>`}
<h2>Where the late tee times are in ${esc(name)}</h2>
<p>Twilight is the cheapest golf most courses sell: the same holes at a lower rate because you may not finish all of them before dark. These ${esc(name)} courses run their tee sheets latest, based on the last 28 days — the right places to look for an after-work nine or a discounted eighteen.</p>
${late.length ? `<ul class="list">${late.map((o) => `<li><a href="${courseHref(o.c)}">${esc(o.c.name)}</a> <small>${esc(o.c.city || "")} · last tee times around ${minsToClock(o.s.mins_late)}${o.s.price_med ? ` · usually about ${money(Math.round(o.s.price_med))}` : ""}</small></li>`).join("")}</ul>` : `<p class="sub">Not enough recent sheet data to rank late tee times yet.</p>`}
<p class="note"><strong>Planning the weekend?</strong> A free OneTee account filters by time of day; Premium shows twilight slots for the next 30 days. <a href="${widgetHref(st)}">Open OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `Twilight tee times in ${name} today — late afternoon golf, cheapest first — OneTee`,
    desc: `Open twilight and late-afternoon tee times across ${name}'s public courses today, cheapest course first, plus the courses whose sheets run latest. Book direct, no fees.`,
    canonical: SITE + twilightHref(st), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, { label: "Twilight" }], body });
}

// The badge a course can put on its own site. Plain SVG, system fonts, brand colours.
const BADGE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="220" height="48" viewBox="0 0 220 48" role="img" aria-label="Tee times on OneTee"><rect width="220" height="48" rx="10" fill="#3b4f5c"/><circle cx="26" cy="24" r="11" fill="#f4f1ea"/><circle cx="26" cy="24" r="4" fill="#6C844C"/><text x="46" y="21" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" font-size="11" fill="#c9d3c0">Today's open</text><text x="46" y="37" font-family="Georgia,Times New Roman,serif" font-size="15" font-weight="700" fill="#ffffff">Tee times on OneTee</text></svg>`;
const badgeSnippet = (c) => `<a href="${SITE}${courseHref(c)}" title="${esc(c.name)} tee times on OneTee"><img src="${SITE}/badge.svg" alt="${esc(c.name)} tee times on OneTee" width="220" height="48"></a>`;
function operatorBox(c) {
  const snip = badgeSnippet(c);
  return `<div class="note" id="operators"><strong>Run ${esc(c.name)}?</strong> OneTee sends golfers to your own booking page and adds no fees. Put this badge on your site and it links straight to your <a href="${courseHref(c)}">OneTee page</a>, where your open tee times are already listed:
<pre class="snip">${esc(snip)}</pre><small>Photo wrong, phone out of date, or a better booking link? Email <a href="mailto:support@oneteeapp.com?subject=${encodeURIComponent("Course page: " + c.name)}">support@oneteeapp.com</a>. <a href="/for-courses/">More for course operators →</a></small></div>`;
}

function renderForCourses(model) {
  const pub = model.courses.filter(isPublic).length, online = model.courses.filter((c) => c.booking_method === "online").length;
  const sample = model.courses.find((c) => c.venue_id === "applewood-golf-course") || model.courses.find((c) => isPublic(c) && c.photo) || model.courses.find(isPublic);
  const body = `<h1>OneTee for golf courses</h1>
<p class="sub">Free listing, free traffic, no commission. Here is what OneTee does with your tee sheet and how to make the most of it.</p>
<h2>What OneTee is</h2>
<p>OneTee reads the public tee sheets of ${pub.toLocaleString("en-US")} courses across ${model.states.length} states — ${online.toLocaleString("en-US")} of them online through ForeUp, TeeItUp, Chronogolf, GolfNow, EZLinks, Club Prophet and the rest — and puts every open slot on one searchable page. When a golfer picks a time, we send them to <em>your</em> booking page. We never take the booking, never mark up the price and never charge the course. Your listing is free, and it stays free.</p>
<h2>What you get</h2>
<ul class="list">
<li><b>A page for your course</b> <small>— today's open times, typical prices, your phone, website and booking link, and a photo from your own site.</small></li>
<li><b>Golfers who were going to book anyway</b> <small>— people searching "tee times near me" or your town's name land on OneTee and click through to book with you.</small></li>
<li><b>Your empty slots in front of people</b> <small>— twilight, midweek and last-minute openings are exactly what OneTee surfaces.</small></li>
<li><b>No integration</b> <small>— if your tee sheet is online, you're probably already listed. Nothing to install.</small></li>
</ul>
<h2>Link to your OneTee page</h2>
<p>A link from your site to your OneTee page helps golfers find your open times through OneTee and helps your page rank. Paste this where you list your booking options — swap in your own course page address (find it on <a href="/">the state pages</a>):</p>
${sample ? `<pre class="snip">${esc(badgeSnippet(sample))}</pre><p><a href="${courseHref(sample)}"><img src="/badge.svg" alt="Tee times on OneTee" width="220" height="48"></a></p>` : ""}
<p>Prefer text? "Find our open tee times on <a href="${MAIN}/">OneTee</a>" works too.</p>
<h2>Fix or improve your listing</h2>
<p>Wrong phone, old website, a better booking link, a photo you'd rather we used, or a course that's missing altogether: email <a href="mailto:support@oneteeapp.com?subject=${encodeURIComponent("Course listing")}">support@oneteeapp.com</a> with the course name and what to change. Most fixes are live within a day.</p>
<h2>Private clubs</h2>
<p>Private and members-only clubs are listed by name so golfers know they're private; we show no tee times for them. If we have you wrong, tell us.</p>
<p class="note"><strong>Questions?</strong> <a href="${MAIN}/about">About OneTee</a> · <a href="${MAIN}/contact">Contact</a></p>`;
  return layout({ title: "OneTee for golf courses — free listing, direct bookings, no fees", desc: "How OneTee lists your public golf course: your open tee times in front of golfers, booked on your own site, with no commission and nothing to install. Plus a badge to link to your page.",
    canonical: SITE + "/for-courses/", crumbs: [{ label: "Tee times", href: "/" }, { label: "For golf courses" }], body });
}

function renderTraffic() {
  const body = `<h1>Traffic — ${esc(HOST)}</h1>
<p class="sub">Page views from the beacon on every page (no cookies, no IPs). Bots are counted separately. <select id="days"><option value="7">Last 7 days</option><option value="14">14 days</option><option value="30">30 days</option><option value="90">90 days</option></select></p>
<div id="out">Loading…</div>
<script>
(function(){
  var esc=function(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})};
  function load(days){
    fetch("/api/traffic?days="+days,{cache:"no-store"}).then(function(r){return r.json()}).then(function(j){
      var t=j.totals||{}; var bots=(j.bot_by_day||[]).reduce(function(a,b){return a+Number(b.views)},0);
      var h='<ul class="facts"><li><b>Views</b>'+(t.views||0)+'</li><li><b>Visits</b>'+(t.visits||0)+'</li><li><b>Mobile</b>'+(t.views?Math.round(100*t.mobile/t.views):0)+'%</li><li><b>Bot views</b>'+bots+'</li></ul>';
      h+='<h2>By day</h2><table class="guide"><tr><th>Day</th><th>Views</th><th>Visits</th><th>Bot views</th></tr>';
      var bm={}; (j.bot_by_day||[]).forEach(function(b){bm[b.day]=b.views});
      (j.by_day||[]).forEach(function(d){h+='<tr><td>'+esc(d.day)+'</td><td>'+d.views+'</td><td>'+d.visits+'</td><td>'+(bm[d.day]||0)+'</td></tr>'});
      h+='</table><h2>Top pages</h2><table class="guide"><tr><th>Path</th><th>Views</th><th>Visits</th></tr>';
      (j.top_paths||[]).forEach(function(p){h+='<tr><td><a href="'+esc(p.path)+'">'+esc(p.path)+'</a></td><td>'+p.views+'</td><td>'+p.visits+'</td></tr>'});
      h+='</table><h2>Referrers</h2><table class="guide"><tr><th>Site</th><th>Views</th><th>Visits</th></tr>';
      (j.referrers||[]).forEach(function(p){h+='<tr><td>'+esc(p.ref)+'</td><td>'+p.views+'</td><td>'+p.visits+'</td></tr>'});
      h+='</table>'; document.getElementById("out").innerHTML=h;
    }).catch(function(e){document.getElementById("out").textContent="Could not load: "+e});
  }
  var sel=document.getElementById("days"); sel.addEventListener("change",function(){load(sel.value)}); load(7);
})();
</script>`;
  return layout({ title: "Traffic — OneTee pages", desc: "Private traffic view.", canonical: SITE + "/_traffic/", crumbs: [], body, noindex: true, beacon: false });
}

// ---------- IndexNow (Bing, Yandex, DuckDuckGo via Bing) ----------
// The key lives in a file we also serve at /<key>.txt. Every build submits the
// hub pages (index, states, deals, twilight); once a day the whole indexable set.
const INDEXNOW_KEY_FILE = process.env.INDEXNOW_KEY_FILE || "";
const INDEXNOW_STAMP = process.env.INDEXNOW_STAMP || "";
function indexNowKey() {
  if (!INDEXNOW_KEY_FILE) return "";
  try { const k = fs.readFileSync(INDEXNOW_KEY_FILE, "utf8").trim(); if (/^[a-f0-9]{32}$/.test(k)) return k; } catch (e) { /* create */ }
  const k = [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");
  try { fs.writeFileSync(INDEXNOW_KEY_FILE, k + "\n", { mode: 0o600 }); return k; } catch (e) { console.error("indexnow key:", e.message); return ""; }
}
function postJSON(url, body) {
  return new Promise((resolve) => {
    const data = JSON.stringify(body);
    const u = new URL(url);
    const req = https.request({ hostname: u.hostname, path: u.pathname, method: "POST", headers: { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(data), "User-Agent": "onetee-pages/1" } },
      (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve({ status: res.statusCode, body: d.slice(0, 200) })); });
    req.on("error", (e) => resolve({ status: 0, body: e.message }));
    req.setTimeout(20000, () => req.destroy(new Error("timeout")));
    req.end(data);
  });
}
async function submitIndexNow(key, hubUrls, allUrls) {
  if (!key || FIXTURE) return "skipped";
  let full = false;
  try { const last = Number(fs.readFileSync(INDEXNOW_STAMP, "utf8")); full = !(last > 0) || Date.now() - last > 20 * 3600 * 1000; } catch (e) { full = true; }
  const list = (full ? allUrls : hubUrls).slice(0, 10000);
  const r = await postJSON("https://api.indexnow.org/indexnow", { host: HOST, key, keyLocation: `${SITE}/${key}.txt`, urlList: list });
  if (full && r.status >= 200 && r.status < 300 && INDEXNOW_STAMP) { try { fs.writeFileSync(INDEXNOW_STAMP, String(Date.now())); } catch (e) { /* ignore */ } }
  return `${full ? "full" : "hubs"} ${list.length} urls -> HTTP ${r.status}${r.body ? " " + r.body.replace(/\s+/g, " ") : ""}`;
}

// ---------- write ----------
function writePage(root, href, html) {
  const dir = path.join(root, decodeURIComponent(href));
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "index.html"), html);
}

async function main() {
  const t0 = Date.now();
  const dir = await loadDirectory();
  const states = [...new Set(dir.map((c) => c.state).filter((s) => STATE_NAMES[s]))].sort();
  const times = await loadTimes(states, dir);
  const stats = await loadStats(states, dir);
  const model = buildModel(dir, times, stats);
  MODEL = model;
  const stamp = localClock("America/Denver") + " on " + fmtDate(localDate("America/Denver"));
  const tmp = OUT + ".tmp";
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });

  // Every page is written; only the ones worth indexing go in the sitemap (the rest
  // carry noindex,follow so their links still count).
  const urls = [];
  let pages = 0;
  const add = (href, priority, index = true) => { pages++; if (index) urls.push({ href, priority }); };
  writePage(tmp, "/", renderIndex(model, stamp)); add("/", "1.0");
  for (const st of model.states) { writePage(tmp, stateHref(st), renderState(model, st, stamp)); add(stateHref(st), "0.9"); }
  for (const x of model.cities.values()) { writePage(tmp, cityHref(x.state, x.slug), renderCity(model, x, stamp)); add(cityHref(x.state, x.slug), "0.6", cityIndexable(x)); }
  for (const c of model.courses) { writePage(tmp, courseHref(c), renderCourse(model, c, stamp)); add(courseHref(c), model.byVenue.has(c.venue_id) ? "0.7" : "0.4", courseIndexable(c)); }
  // Deals and twilight: one each per state, plus "cheap golf in <city>" where a town has the data to rank.
  const hubs = ["/"];
  for (const st of model.states) {
    writePage(tmp, dealsHref(st), renderDeals(model, st, stamp)); add(dealsHref(st), "0.8"); hubs.push(stateHref(st), dealsHref(st), twilightHref(st));
    writePage(tmp, twilightHref(st), renderTwilight(model, st, stamp)); add(twilightHref(st), "0.8");
  }
  let cityDeals = 0;
  for (const x of model.cities.values()) if (cityDealsEligible(model, x)) { writePage(tmp, cityDealsHref(x.state, x.slug), renderCityDeals(model, x, stamp)); add(cityDealsHref(x.state, x.slug), "0.7"); cityDeals++; }
  writePage(tmp, "/for-courses/", renderForCourses(model)); add("/for-courses/", "0.6");
  writePage(tmp, "/_traffic/", renderTraffic()); pages++;
  fs.writeFileSync(path.join(tmp, "badge.svg"), BADGE_SVG);
  const inKey = indexNowKey();
  if (inKey) fs.writeFileSync(path.join(tmp, `${inKey}.txt`), inKey);

  const today = new Date().toISOString().slice(0, 10);
  fs.writeFileSync(path.join(tmp, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.map((u) => `<url><loc>${esc(SITE + u.href)}</loc><lastmod>${today}</lastmod><changefreq>${u.priority >= "0.7" ? "hourly" : "daily"}</changefreq><priority>${u.priority}</priority></url>`).join("\n") + `\n</urlset>\n`);
  fs.writeFileSync(path.join(tmp, "robots.txt"), `User-agent: *\nAllow: /\nDisallow: /_traffic/\nDisallow: /api/\nSitemap: ${SITE}/sitemap.xml\n`);
  fs.writeFileSync(path.join(tmp, "404.html"), layout({ title: "Not found — OneTee", desc: "That page is not here.", canonical: SITE + "/", crumbs: [],
    body: `<h1>That page is not here</h1><p class="sub">Try <a href="/">tee times by state</a> or <a href="${MAIN}/tee-times">search on OneTee</a>.</p>` }));
  const live = [...model.byVenue.keys()].length;
  const totalTimes = [...model.byVenue.values()].reduce((s, v) => s + v.count, 0);
  const rolled = model.states.filter((st) => dayFor(st).rolled);
  fs.writeFileSync(path.join(tmp, "_build.json"), JSON.stringify({ built_at: new Date().toISOString(), pages, indexable: urls.length, states: model.states,
    showing_tomorrow: rolled, courses: model.courses.length, cities: model.cities.size, courses_with_times: live, tee_times: totalTimes,
    courses_with_stats: [...model.stats.values()].filter(statsUsable).length, courses_with_photo: model.courses.filter((c) => c.photo).length,
    city_guides: [...model.cities.values()].filter((x) => x.courses.filter(isPublic).length >= 2).length, city_deals: cityDeals,
    seconds: Math.round((Date.now() - t0) / 100) / 10 }, null, 2));

  // atomic swap
  const old = OUT + ".old";
  fs.rmSync(old, { recursive: true, force: true });
  if (fs.existsSync(OUT)) fs.renameSync(OUT, old);
  fs.renameSync(tmp, OUT);
  fs.rmSync(old, { recursive: true, force: true });
  const inRes = await submitIndexNow(inKey, hubs.map((h) => SITE + h), urls.map((u) => SITE + u.href));
  console.log("indexnow:", inRes);
  try { fs.writeFileSync(path.join(OUT, "_indexnow.json"), JSON.stringify({ at: new Date().toISOString(), result: inRes })); } catch (e) { /* ignore */ }
  console.log(`pages: ${pages} written (${urls.length} in the sitemap) to ${OUT} — ${model.states.length} states, ${model.cities.size} cities, ${model.courses.length} courses (${live} with ${totalTimes} tee times${rolled.length ? `; ${rolled.join(",")} showing tomorrow` : ""}) in ${Math.round((Date.now() - t0) / 1000)}s`);
}

main().catch((e) => { console.error("build failed:", e); process.exit(1); });
