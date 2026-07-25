/* OneTee — show the courses we DON'T have tee times for, underneath the ones
 * we do, each tagged with how a golfer can actually book it.
 *
 * The widget is a Squarespace code component and is not in this repo, so this
 * is a drop-in file plus ONE line of integration, not a rewrite.
 *
 * ---------------------------------------------------------------------------
 * THE PROBLEM THIS SOLVES
 * ---------------------------------------------------------------------------
 * "No results" and "we have never heard of that course" look identical to a
 * golfer, and only one of them is true. Search Colorado Springs today and The
 * Broadmoor is absent — not because it is missing from our data, but because
 * it does not sell tee times online. The golfer cannot tell those apart, so
 * the reasonable conclusion is that OneTee's coverage is thin.
 *
 * So every course we know of appears. The ones we can book come first, live,
 * with times. The rest follow, greyed, each saying what it actually takes to
 * play there and linking somewhere the golfer can act.
 *
 *   Book on course site   we know it books online, we just don't carry it yet
 *   Call to book          no online tee sheet — the pro shop's number is here
 *   Private club          members only. Listed so "missing" reads as "closed"
 *   Unconfirmed           we could not establish either way; here is its site
 *
 * ---------------------------------------------------------------------------
 * INSTALLING IT (three steps, one of which is a single line)
 * ---------------------------------------------------------------------------
 * 1. Paste this whole file into the widget's <script>, anywhere above the
 *    code that renders results.
 * 2. Set API_BASE below if the Worker URL ever changes.
 * 3. At the END of whatever function paints the results list — after the last
 *    live course has been appended — add one line:
 *
 *       OneTeeDirectory.render({ container: resultsEl, teeTimes: rows,
 *                                state: selectedState, city: selectedCity,
 *                                query: searchText });
 *
 *    `container` is the element the live cards were appended to. `teeTimes` is
 *    the array the widget just rendered; that is how this knows which courses
 *    are already on screen. The other three are whatever the current filters
 *    are — pass "" or omit any the widget does not have.
 *
 * Calling it again re-renders in place; it removes its own previous output
 * first, so it is safe to call on every paint.
 *
 * ---------------------------------------------------------------------------
 * DESIGN NOTES WORTH KEEPING
 * ---------------------------------------------------------------------------
 * The directory does NOT record whether OneTee currently has tee times for a
 * course, and it must not. That is a live fact that changes hourly; a copy of
 * it baked into a bundled file would be a badge that is wrong between deploys.
 * Liveness is decided here, at render time, by what is on screen.
 *
 * That is also why a course tagged `online` renders as "Book on course site"
 * rather than "Book online": by the time it reaches this section we know we
 * are NOT the one serving it, and the honest thing to say is where to go
 * instead. The data stays a plain fact; the wording that depends on context
 * lives where the context is.
 *
 * Styles are scoped and self-contained (`.ot-dir-*`), injected once. The
 * cards should look deliberate next to whatever the widget's own CSS does,
 * without inheriting or fighting it.
 */
(function (global) {
  "use strict";

  var API_BASE = "https://onetee-api.damp-snow-8025.workers.dev";

  // Order the sections appear in: most actionable first. A golfer who cannot
  // get a time from us is best served by a course that will still sell them
  // one today, and worst served by a private club, which is information
  // rather than an option.
  var ORDER = ["online", "phone", "unknown", "private"];

  var HEADING = {
    online: "Books on its own site",
    phone: "Call the pro shop",
    unknown: "Booking method unconfirmed",
    private: "Private clubs",
  };

  // Deliberately different from the directory's own `label` for `online` —
  // see the design note above.
  var TAG = {
    online: "Book on course site",
    phone: "Call to book",
    unknown: "Check course site",
    private: "Members only",
  };

  // Short on purpose. The pill beside it already says what kind of booking
  // this is, and repeating "Book on course site" in both places reads as a
  // stutter rather than as emphasis.
  var ACTION = {
    online: "Book",
    phone: "Course site",
    unknown: "Course site",
    private: "Course site",
  };

  var CSS = [
    ".ot-dir{margin-top:28px;border-top:1px solid rgba(0,0,0,.12);padding-top:18px}",
    ".ot-dir-intro{font-size:14px;line-height:1.5;opacity:.7;margin:0 0 16px}",
    ".ot-dir-h{font-size:12px;letter-spacing:.08em;text-transform:uppercase;",
    "opacity:.55;margin:18px 0 8px;font-weight:600}",
    ".ot-dir-card{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 12px;",
    "padding:10px 0;border-bottom:1px solid rgba(0,0,0,.06);opacity:.72}",
    ".ot-dir-card:hover{opacity:1}",
    ".ot-dir-name{font-weight:600;flex:1 1 220px}",
    ".ot-dir-loc{font-size:13px;opacity:.7}",
    ".ot-dir-tag{font-size:11px;letter-spacing:.04em;text-transform:uppercase;",
    "border:1px solid currentColor;border-radius:999px;padding:2px 9px;opacity:.75}",
    ".ot-dir-links{display:flex;gap:14px;font-size:13px;white-space:nowrap}",
    ".ot-dir-links a{text-decoration:underline}",
    ".ot-dir-more{font-size:13px;opacity:.7;margin-top:12px}",
  ].join("");

  var cache = null;          // the fetched directory, kept for the session
  var inflight = null;       // so ten renders in a row make one request

  function injectCSS() {
    if (document.getElementById("ot-dir-css")) return;
    var s = document.createElement("style");
    s.id = "ot-dir-css";
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function load() {
    if (cache) return Promise.resolve(cache);
    if (inflight) return inflight;
    // No state filter on the request: one fetch covers every filter the user
    // can then click through, and the response is cached by the browser for
    // an hour. Refetching per state would be more requests for less.
    inflight = fetch(API_BASE + "/api/directory")
      .then(function (r) { return r.json(); })
      .then(function (d) { cache = (d && d.courses) || []; return cache; })
      .catch(function () { cache = []; return cache; });   // stay silent: the
    return inflight;                                       // live list is fine
  }                                                        // without us

  function norm(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* Which venues are already on screen. Primary key is venue_id, which the
   * API hands out as course_slug and the directory mirrors exactly. Name+state
   * is a second, cheaper key: if those two ever drift, the visible failure is
   * a course listed twice — once live and once greyed — and that is worth
   * spending a Set to prevent. */
  function shownKeys(teeTimes) {
    var seen = Object.create(null);
    (teeTimes || []).forEach(function (t) {
      if (!t) return;
      var id = t.venue_id || t.course_slug;
      if (id) seen["id:" + id] = 1;
      // course_name may have a sub-course appended ("Foo · Back 9"); the part
      // before the separator is the facility, which is what the directory has.
      var nm = String(t.course_name || "").split("·")[0];
      if (nm) seen["nm:" + (t.state || "") + norm(nm)] = 1;
    });
    return seen;
  }

  function card(c) {
    var loc = [c.city, c.state].filter(Boolean).join(", ");
    var links = [];
    if (c.phone) {
      links.push('<a href="tel:' + esc(c.phone.replace(/[^0-9+]/g, "")) +
                 '">' + esc(c.phone) + "</a>");
    }
    if (c.action_url) {
      links.push('<a href="' + esc(c.action_url) + '" target="_blank" ' +
                 'rel="noopener noreferrer">' +
                 esc(ACTION[c.booking_method] || "Visit course site") + "</a>");
    }
    return '<div class="ot-dir-card">' +
             '<span class="ot-dir-name">' + esc(c.name) + "</span>" +
             '<span class="ot-dir-loc">' + esc(loc) + "</span>" +
             '<span class="ot-dir-tag">' +
                esc(TAG[c.booking_method] || c.label) + "</span>" +
             '<span class="ot-dir-links">' + links.join("") + "</span>" +
           "</div>";
  }

  function render(opts) {
    opts = opts || {};
    var container = opts.container;
    if (!container) return Promise.resolve(0);

    return load().then(function (all) {
      injectCSS();
      var old = container.querySelector(".ot-dir");
      if (old) old.remove();

      var st = (opts.state || "").toUpperCase();
      var city = (opts.city || "").toLowerCase();
      var q = (opts.query || "").toLowerCase();
      var seen = shownKeys(opts.teeTimes);

      var rest = all.filter(function (c) {
        if (st && c.state !== st) return false;
        if (city && String(c.city || "").toLowerCase() !== city) return false;
        if (q && String(c.name || "").toLowerCase().indexOf(q) === -1) return false;
        if (seen["id:" + c.venue_id]) return false;
        if (seen["nm:" + c.state + norm(c.name)]) return false;
        return true;
      });
      if (!rest.length) return 0;

      var groups = {};
      rest.forEach(function (c) {
        var k = ORDER.indexOf(c.booking_method) === -1 ? "unknown"
                                                       : c.booking_method;
        (groups[k] || (groups[k] = [])).push(c);
      });

      var html = ['<div class="ot-dir">',
        '<p class="ot-dir-intro">', String(rest.length),
        rest.length === 1 ? " more course" : " more courses",
        " here that we can't book for you right now — this is how to reach ",
        rest.length === 1 ? "it" : "them", " directly.</p>"];

      ORDER.forEach(function (k) {
        var list = groups[k];
        if (!list || !list.length) return;
        list.sort(function (a, b) {
          return (a.name || "").localeCompare(b.name || "");
        });
        html.push('<div class="ot-dir-h">', esc(HEADING[k]),
                  " · ", String(list.length), "</div>");
        list.forEach(function (c) { html.push(card(c)); });
      });

      html.push("</div>");
      var wrap = document.createElement("div");
      wrap.innerHTML = html.join("");
      container.appendChild(wrap.firstChild);
      return rest.length;
    });
  }

  global.OneTeeDirectory = { render: render, load: load, API_BASE: API_BASE };
})(window);
