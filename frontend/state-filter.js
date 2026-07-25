/* OneTee tee-times widget — retire the stateOf() city-inference hack.
 *
 * The widget is a Squarespace code component on /tee-times and is not in this
 * repo, so this file is a drop-in replacement for three named pieces of it
 * rather than the whole thing. Replace the pieces, delete what the comments
 * say to delete, change nothing else.
 *
 * ---------------------------------------------------------------------------
 * WHY THE OLD HELPER HAD TO GO, AND WHY IT WAS SAFE TO WRITE IN THE FIRST PLACE
 * ---------------------------------------------------------------------------
 * stateOf(t) was written when the API returned no `state` field at all. It
 * guessed: a hardcoded set of ~71 Arizona city names meant AZ, a course-name
 * exception handled Florence (Poston Butte is the AZ one), and anything
 * unrecognised fell through to CO. That was the right call at the time — the
 * data genuinely did not carry the answer.
 *
 * Three things are wrong with keeping it now that the data does:
 *
 *   1. UNKNOWN DEFAULTS TO COLORADO. Any city not in the AZ set is silently
 *      labelled CO. That is not a display quirk; it puts a course in the wrong
 *      state's filtered list, and the golfer never sees a course they were
 *      looking for.
 *   2. IT NEEDS A CODE CHANGE PER STATE. The moment Utah data lands, every UT
 *      course renders as Colorado until someone edits this file. The state
 *      dropdown below is built from the data instead, so a new state appears
 *      on its own.
 *   3. IT MASKS DATA BUGS. A row with no state used to look identical to a
 *      Colorado row. That is exactly how three retired ARIZONA courses
 *      (gold-canyon-...-sidewinder, grayhawk-...-raptor-talon,
 *      mountain-view-...-fort-huachuca — 135 rows, dead slugs left active in
 *      D1) sat in the Colorado list unnoticed. They are now deactivated at the
 *      data layer, and with the guess gone a future gap shows up as "—"
 *      instead of quietly becoming Colorado.
 *
 * The one thing that must NOT change with it: a row whose state is missing has
 * to stay VISIBLE. Hiding it would turn a data gap into a missing tee time.
 * It shows under "All states", is excluded only from a specific state's
 * filtered view, and renders its location as the bare city name.
 */

/* ===========================================================================
 * 1. REPLACE the whole stateOf() function — and DELETE the AZ_CITIES set and
 *    the Florence / Poston Butte exception that only existed to feed it.
 * ======================================================================== */

/** The state for a tee-time row, straight from the API. "" when absent.
 *  No inference, no default: an unknown state is unknown, not Colorado. */
function stateOf(t) {
  return (t && t.state ? String(t.state).trim().toUpperCase() : '');
}

/* ===========================================================================
 * 2. REPLACE the hardcoded state dropdown options (the All states / Colorado /
 *    Arizona list) with these two helpers, and call fillStateOptions() once
 *    after the first fetch resolves.
 * ======================================================================== */

/* Full names for the dropdown label. A state missing from this map still works
 * — it just shows its two-letter code — so landing in a new state is a data
 * event, not a release. */
const STATE_NAMES = {
  AL: 'Alabama', AK: 'Alaska', AZ: 'Arizona', AR: 'Arkansas',
  CA: 'California', CO: 'Colorado', CT: 'Connecticut', DE: 'Delaware',
  DC: 'District of Columbia', FL: 'Florida', GA: 'Georgia', HI: 'Hawaii',
  ID: 'Idaho', IL: 'Illinois', IN: 'Indiana', IA: 'Iowa', KS: 'Kansas',
  KY: 'Kentucky', LA: 'Louisiana', ME: 'Maine', MD: 'Maryland',
  MA: 'Massachusetts', MI: 'Michigan', MN: 'Minnesota', MS: 'Mississippi',
  MO: 'Missouri', MT: 'Montana', NE: 'Nebraska', NV: 'Nevada',
  NH: 'New Hampshire', NJ: 'New Jersey', NM: 'New Mexico', NY: 'New York',
  NC: 'North Carolina', ND: 'North Dakota', OH: 'Ohio', OK: 'Oklahoma',
  OR: 'Oregon', PA: 'Pennsylvania', RI: 'Rhode Island', SC: 'South Carolina',
  SD: 'South Dakota', TN: 'Tennessee', TX: 'Texas', UT: 'Utah',
  VT: 'Vermont', VA: 'Virginia', WA: 'Washington', WV: 'West Virginia',
  WI: 'Wisconsin', WY: 'Wyoming'
};

function stateName(code) {
  return STATE_NAMES[code] || code;
}

/** Build the STATE dropdown from the states actually present in the feed,
 *  preserving the current selection if it is still available. */
function fillStateOptions(rows, selectEl) {
  const present = Array.from(new Set(rows.map(stateOf).filter(Boolean))).sort();
  const keep = selectEl.value;
  selectEl.innerHTML = '<option value="">All states</option>' +
    present.map(function (c) {
      return '<option value="' + c + '">' + stateName(c) + '</option>';
    }).join('');
  if (keep && present.indexOf(keep) !== -1) selectEl.value = keep;
}

/* ===========================================================================
 * 3. REPLACE the state filter predicate and the two places that render a
 *    location as "City, ST".
 * ======================================================================== */

/** Rows for the selected state. "" (All states) keeps everything, INCLUDING
 *  rows with no state — a data gap must never hide a bookable tee time. */
function filterByState(rows, sel) {
  return sel ? rows.filter(function (t) { return stateOf(t) === sel; }) : rows;
}

/** "Englewood, CO" — or just "Englewood" when the state is unknown, rather
 *  than inventing one. Used by both the course cards and the by-time table. */
function locationOf(t) {
  const city = (t.city || '').trim();
  const st = stateOf(t);
  if (city && st) return city + ', ' + st;
  return city || st || '';
}

/* ===========================================================================
 * NOTES ON THE REST OF THE WIDGET — no changes needed, but worth knowing.
 * ---------------------------------------------------------------------------
 * ?state= ON LOAD keeps working unchanged: it sets the dropdown value before
 * the first fetch, and fillStateOptions() above preserves that selection as
 * long as the state is present in the data. If it is not (someone links to
 * ?state=UT before Utah data lands) the widget falls back to All states, which
 * is the right failure — an empty list with a state selected looks broken.
 *
 * CITY_LL (the ~71 AZ cities with lat/lng) is a DIFFERENT map and must stay.
 * It powers the NEAR + radius filter, not the state guess. Only the AZ_CITIES
 * name set used by the old stateOf() is deleted.
 *
 * SERVER-SIDE ?state= — the API supports it and returns nothing but that
 * state. Not used here on purpose: the widget needs the full result set to
 * populate the state, city and course dropdowns, so it filters client-side
 * from one fetch. Worth revisiting only if row counts grow past a few
 * thousand.
 *
 * autoAdv (the one-time hop to the next day when the first filtered result set
 * is empty) is unaffected — it runs on the filtered rows either way.
 * ======================================================================== */
