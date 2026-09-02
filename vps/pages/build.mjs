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
// Env: API_BASE (default http://127.0.0.1:8080), PAGES_OUT (default
// /var/www/onetee-pages), PAGES_HOST (default tee-times.oneteeapp.com),
// FIXTURE=1 renders from the bundled directory with synthetic times (local dev).

import http from "node:http";
import https from "node:https";
import fs from "node:fs";
import path from "node:path";

const API = process.env.API_BASE || "http://127.0.0.1:8080";
const OUT = process.env.PAGES_OUT || "/var/www/onetee-pages";
const HOST = process.env.PAGES_HOST || "tee-times.oneteeapp.com";
const SITE = "https://" + HOST;
const MAIN = "https://www.oneteeapp.com";
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
      const day = localDate(tzOf(c.state));
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
    const day = localDate(tzOf(st));
    const j = await getJSON(`${API}/api/tee-times?state=${st}&date=${day}&limit=25000`);
    byState[st] = j.tee_times || [];
  }
  return byState;
}

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
footer{border-top:1px solid var(--line);color:var(--ink2);font-size:13px;padding:22px 20px;text-align:center}
@media(max-width:640px){h1{font-size:26px}.list{columns:1}header.top nav a{margin-left:10px}}
`;

function layout({ title, desc, canonical, crumbs, body, jsonld }) {
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
<meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(desc)}"><meta property="og:url" content="${esc(canonical)}"><meta property="og:site_name" content="OneTee">
<style>${CSS}</style>
${lds}
</head>
<body>
<header class="top"><div class="in"><a class="brand" href="${MAIN}/">OneTee</a><nav><a href="${SITE}/">By state</a><a href="${MAIN}/map">Map</a><a class="cta" href="${MAIN}/tee-times">Search tee times</a></nav></div></header>
<main>
${crumbHtml}
${body}
</main>
<footer>OneTee gathers public golf tee times from course booking sites. You book directly with the course; OneTee adds no fees. · <a href="${MAIN}/about">About</a> · <a href="${MAIN}/contact">Contact</a> · <a href="${MAIN}/roadmap">Roadmap</a></footer>
</body>
</html>
`;
}

// ---------- model ----------
function buildModel(dir, timesByState) {
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
  return { states: [...new Set(courses.map((c) => c.state))].sort(), courses, cities, byVenue };
}

// ---------- renderers ----------
const courseHref = (c) => `/course/${encodeURIComponent(c.venue_id)}/`;
const stateHref = (st) => `/${stateSlug(st)}/`;
const cityHref = (st, citySlug) => `/${stateSlug(st)}/${citySlug}/`;
const widgetHref = (st) => `${MAIN}/tee-times?state=${st}`;

function timeChip(r) {
  const label = r.course_label ? ` · ${esc(r.course_label)}` : "";
  const inner = `<b>${fmtTime(r.teetime)}</b><small>${[priceRange(r.price_min, r.price_max), r.open_spots ? plural(r.open_spots, "spot") : "", r.holes ? `${esc(r.holes)} holes` : ""].filter(Boolean).join(" · ")}${label}</small>`;
  return r.booking_url ? `<li><a href="${esc(r.booking_url)}" rel="nofollow noopener" target="_blank">${inner}</a></li>` : `<li>${inner}</li>`;
}

function courseCard(c, v, { max = 8 } = {}) {
  const rows = v ? v.rows : [];
  const shown = rows.slice(0, max);
  const meta = [c.city ? `${esc(c.city)}, ${esc(c.state)}` : esc(c.state), c.type ? esc(c.type) : "",
    v ? plural(v.count, "tee time") + " today" : esc(c.label || ""), v && v.fromPrice != null ? `from ${money(v.fromPrice)}` : ""].filter(Boolean).join(" · ");
  return `<article class="course"><h3><a href="${courseHref(c)}">${esc(c.name)}</a></h3><div class="meta">${meta}</div>` +
    (shown.length ? `<ul class="times">${shown.map(timeChip).join("")}</ul>` : "") +
    (rows.length > shown.length ? `<div class="more"><a href="${courseHref(c)}">+${rows.length - shown.length} more today</a></div>` : "") +
    (!rows.length && c.phone ? `<div class="more">${c.booking_method === "phone" ? "Call to book: " : "Phone: "}<a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a></div>` : "") +
    `</article>`;
}

function renderIndex(model, stamp) {
  const items = model.states.map((st) => {
    const cs = model.courses.filter((c) => c.state === st);
    const live = cs.filter((c) => model.byVenue.has(c.venue_id));
    const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
    return `<li class="tile"><a class="t" href="${stateHref(st)}">${esc(STATE_NAMES[st])}</a><div class="m">${plural(n, "tee time")} today at ${plural(live.length, "course")} · ${cs.length} courses listed</div></li>`;
  }).join("");
  const body = `<h1>Public golf tee times today, by state</h1>
<p class="sub">Every bookable tee time we can find at public courses, in one place, updated through the day. Pick a state, or <a href="${MAIN}/tee-times">search with filters</a> for time, price, holes and distance.</p>
<ul class="grid">${items}</ul>
<p class="note"><strong>Planning ahead?</strong> Today's times are free to browse. A free OneTee account adds filters and saved searches; Premium opens the next 30 days for $3 a month. <a href="${MAIN}/tee-times">Start on OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}.</p>`;
  return layout({ title: "Golf tee times today across 12 states — OneTee", desc: "Browse today's public golf tee times by state: every course, every open slot, book direct with the course. No fees, no markup.",
    canonical: SITE + "/", crumbs: [], body,
    jsonld: { "@context": "https://schema.org", "@type": "WebSite", name: "OneTee tee times", url: SITE + "/" } });
}

function renderState(model, st, stamp) {
  const name = STATE_NAMES[st];
  const day = localDate(tzOf(st));
  const cs = model.courses.filter((c) => c.state === st);
  const live = cs.filter((c) => model.byVenue.has(c.venue_id));
  const rest = cs.filter((c) => !model.byVenue.has(c.venue_id));
  const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
  const cities = [...model.cities.values()].filter((x) => x.state === st).sort((a, b) => a.city.localeCompare(b.city));
  const cityList = cities.map((x) => {
    const k = x.courses.filter((c) => model.byVenue.has(c.venue_id)).reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
    return `<li><a href="${cityHref(st, x.slug)}">${esc(x.city)}</a> <small>${x.courses.length} ${x.courses.length === 1 ? "course" : "courses"}${k ? ` · ${k} today` : ""}</small></li>`;
  }).join("");
  const body = `<h1>Golf tee times today in ${esc(name)}</h1>
<p class="sub">${fmtDate(day)} · ${plural(n, "open tee time")} at ${plural(live.length, "public course")} · ${cs.length} courses listed. <a href="${widgetHref(st)}">Filter by time, price, holes or distance on OneTee →</a></p>
${live.length ? `<h2>Courses with tee times today</h2>${live.map((c) => courseCard(c, model.byVenue.get(c.venue_id))).join("")}` : `<p class="note">No open tee times are listed for today right now. Courses often release or free up slots through the day — <a href="${widgetHref(st)}">check the live search</a>, or call a course below.</p>`}
${cities.length ? `<h2>By city</h2><ul class="list">${cityList}</ul>` : ""}
${rest.length ? `<h2>More ${esc(name)} courses</h2><ul class="list">${rest.map((c) => `<li><a href="${courseHref(c)}">${esc(c.name)}</a> <small>${esc(c.city || "")}${c.label ? ` · ${esc(c.label)}` : ""}</small></li>`).join("")}</ul>` : ""}
<p class="note"><strong>Want tomorrow or the weekend?</strong> A free account adds filters and saved searches; Premium members see the next 30 days. <a href="${widgetHref(st)}">Open OneTee →</a></p>
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `${name} golf tee times today (${plural(live.length, "course")}, ${plural(n, "time")}) — OneTee`,
    desc: `Today's open tee times at ${live.length} public golf courses in ${name}, plus ${cs.length} courses with booking links and phone numbers. Book direct, no fees.`,
    canonical: SITE + stateHref(st), crumbs: [{ label: "Tee times", href: "/" }, { label: name }], body,
    jsonld: { "@context": "https://schema.org", "@type": "ItemList", name: `Golf courses in ${name}`, numberOfItems: cs.length,
      itemListElement: cs.slice(0, 200).map((c, i) => ({ "@type": "ListItem", position: i + 1, name: c.name, url: SITE + courseHref(c) })) } });
}

function renderCity(model, x, stamp) {
  const st = x.state, name = STATE_NAMES[st];
  const live = x.courses.filter((c) => model.byVenue.has(c.venue_id));
  const rest = x.courses.filter((c) => !model.byVenue.has(c.venue_id));
  const n = live.reduce((s, c) => s + model.byVenue.get(c.venue_id).count, 0);
  const body = `<h1>Golf tee times in ${esc(x.city)}, ${esc(st)}</h1>
<p class="sub">${fmtDate(localDate(tzOf(st)))} · ${plural(n, "open tee time")} today at ${plural(live.length, "course")} in ${esc(x.city)} · <a href="${stateHref(st)}">all of ${esc(name)}</a> · <a href="${widgetHref(st)}">search nearby on OneTee →</a></p>
${live.map((c) => courseCard(c, model.byVenue.get(c.venue_id))).join("")}
${rest.length ? `<h2>${live.length ? "Other courses" : "Courses"} in ${esc(x.city)}</h2>${rest.map((c) => courseCard(c, null)).join("")}` : ""}
<p class="sub">Times as of ${esc(stamp)}. You book directly with the course.</p>`;
  return layout({ title: `${x.city}, ${st} golf tee times today — OneTee`,
    desc: `Open tee times today at ${x.courses.length === 1 ? "the public golf course" : `${x.courses.length} golf courses`} in ${x.city}, ${name}. Book direct with the course, no fees.`,
    canonical: SITE + cityHref(st, x.slug), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, { label: x.city }], body });
}

function renderCourse(model, c, stamp) {
  const st = c.state, name = STATE_NAMES[st];
  const v = model.byVenue.get(c.venue_id);
  const rows = v ? v.rows : [];
  const day = localDate(tzOf(st));
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
  const body = `<h1>${esc(c.name)} tee times</h1>
<p class="sub">${cityLink}<a href="${stateHref(st)}">${esc(name)}</a>${c.blurb ? ` · ${esc(c.blurb)}` : ""}</p>
<ul class="facts">${facts}</ul>
<h2>Today, ${fmtDate(day)}</h2>
${rows.length ? `<p class="sub">${plural(rows.length, "open tee time")}${v.fromPrice != null ? ` from ${money(v.fromPrice)}` : ""}. Times as of ${esc(stamp)}; book directly with the course.</p>${table}` :
    `<p class="note">No open tee times are listed for today${c.booking_method === "online" ? " right now — the course may release more through the day" : ""}. ${c.booking_method === "phone" && c.phone ? `This course takes bookings by phone: <a href="tel:${esc(c.phone.replace(/[^\d+]/g, ""))}">${esc(c.phone)}</a>.` : ""} ${c.action_url ? `<a href="${esc(c.action_url)}" rel="nofollow noopener" target="_blank">Check the course's booking page →</a>` : ""}</p>`}
<p style="margin:18px 0">${bookBtn ? `<a class="btn" href="${esc(bookBtn)}" rel="nofollow noopener" target="_blank">Book at ${esc(c.name)}</a> ` : ""}<a class="btn alt" href="${widgetHref(st)}">See nearby courses on OneTee</a></p>
<p class="note"><strong>Tomorrow and beyond:</strong> Premium members see the next 30 days at every course; a free account adds filters and saved searches. <a href="${MAIN}/tee-times">Open OneTee →</a></p>`;
  const ld = { "@context": "https://schema.org", "@type": "GolfCourse", name: c.name, url: SITE + courseHref(c),
    ...(c.phone ? { telephone: c.phone } : {}), ...(c.website ? { sameAs: c.website } : {}),
    address: { "@type": "PostalAddress", ...(c.city ? { addressLocality: c.city } : {}), addressRegion: st, ...(c.zip ? { postalCode: c.zip } : {}), addressCountry: "US" },
    ...(c.lat != null && c.lng != null ? { geo: { "@type": "GeoCoordinates", latitude: c.lat, longitude: c.lng } } : {}) };
  return layout({ title: `${c.name} tee times — ${c.city ? c.city + ", " : ""}${st} — OneTee`,
    desc: rows.length ? `${rows.length} open tee times today at ${c.name} in ${c.city || name}${v.fromPrice != null ? `, from ${money(v.fromPrice)}` : ""}. Book direct with the course; no fees.`
      : `${c.name} in ${c.city || name}: tee times, booking link${c.phone ? ", phone" : ""} and website. Book direct with the course; no fees.`,
    canonical: SITE + courseHref(c), crumbs: [{ label: "Tee times", href: "/" }, { label: name, href: stateHref(st) }, ...(c.city ? [{ label: c.city, href: cityHref(st, slug(c.city)) }] : []), { label: c.name }],
    body, jsonld: ld });
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
  const model = buildModel(dir, times);
  const stamp = localClock("America/Denver") + " on " + fmtDate(localDate("America/Denver"));
  const tmp = OUT + ".tmp";
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });

  const urls = [];
  const add = (href, priority) => urls.push({ href, priority });
  writePage(tmp, "/", renderIndex(model, stamp)); add("/", "1.0");
  for (const st of model.states) { writePage(tmp, stateHref(st), renderState(model, st, stamp)); add(stateHref(st), "0.9"); }
  for (const x of model.cities.values()) { writePage(tmp, cityHref(x.state, x.slug), renderCity(model, x, stamp)); add(cityHref(x.state, x.slug), "0.6"); }
  for (const c of model.courses) { writePage(tmp, courseHref(c), renderCourse(model, c, stamp)); add(courseHref(c), model.byVenue.has(c.venue_id) ? "0.7" : "0.4"); }

  const today = new Date().toISOString().slice(0, 10);
  fs.writeFileSync(path.join(tmp, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    urls.map((u) => `<url><loc>${esc(SITE + u.href)}</loc><lastmod>${today}</lastmod><changefreq>${u.priority >= "0.7" ? "hourly" : "daily"}</changefreq><priority>${u.priority}</priority></url>`).join("\n") + `\n</urlset>\n`);
  fs.writeFileSync(path.join(tmp, "robots.txt"), `User-agent: *\nAllow: /\nSitemap: ${SITE}/sitemap.xml\n`);
  fs.writeFileSync(path.join(tmp, "404.html"), layout({ title: "Not found — OneTee", desc: "That page is not here.", canonical: SITE + "/", crumbs: [],
    body: `<h1>That page is not here</h1><p class="sub">Try <a href="/">tee times by state</a> or <a href="${MAIN}/tee-times">search on OneTee</a>.</p>` }));
  const live = [...model.byVenue.keys()].length;
  const totalTimes = [...model.byVenue.values()].reduce((s, v) => s + v.count, 0);
  fs.writeFileSync(path.join(tmp, "_build.json"), JSON.stringify({ built_at: new Date().toISOString(), pages: urls.length, states: model.states,
    courses: model.courses.length, cities: model.cities.size, courses_with_times: live, tee_times: totalTimes, seconds: Math.round((Date.now() - t0) / 100) / 10 }, null, 2));

  // atomic swap
  const old = OUT + ".old";
  fs.rmSync(old, { recursive: true, force: true });
  if (fs.existsSync(OUT)) fs.renameSync(OUT, old);
  fs.renameSync(tmp, OUT);
  fs.rmSync(old, { recursive: true, force: true });
  console.log(`pages: ${urls.length} written to ${OUT} — ${model.states.length} states, ${model.cities.size} cities, ${model.courses.length} courses (${live} with ${totalTimes} tee times today) in ${Math.round((Date.now() - t0) / 1000)}s`);
}

main().catch((e) => { console.error("build failed:", e); process.exit(1); });
