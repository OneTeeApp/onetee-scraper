# Architecture

How OneTee is built and why. For engineers picking up the project. Plain-English
overview is in the root `README.md`; term definitions in `docs/GLOSSARY.md`.

> This supersedes the original July-2026 "Colorado prototype" architecture note.
> The system is now live nationwide-in-progress (CO, AZ, FL, VA, MD, UT) on
> Cloudflare D1 + Workers, scraped by GitHub Actions.

---

## 1. The core insight

There is no single source of tee times. The market is fragmented across ~24
booking platforms, and each course licenses exactly one. That fragmentation is
the whole reason an aggregator is valuable — and it dictates the architecture:
**you integrate with platforms, not courses.** ~24 platform integrations cover
thousands of courses. Each new state is mostly registry work (which course uses
which platform), not new code.

Two facts to internalize early:
- **Platform churn is real.** Courses switch booking platforms over time, and
  platforms change their own APIs/markup without notice (see §7). The system must
  detect breakage, not assume a static world.
- **Portals are leverage.** One municipal portal often covers many courses (one
  MemberSports portal = several city courses). Map portals, not just courses.

---

## 2. Components & data flow

```
  *_golf_courses_booking.csv         (human-curated, one per state)
            │  build_registry.py / build_directory.py
            ▼
  registry.json  +  directory.json    (machine registry / full course directory)
            │  scraper/aggregate.py  (+ scraper/browser_*.py for hard platforms)
            ▼
  normalized TeeTime objects          (scraper/models.py)
            │  scraper/d1.py  (diff-sync)
            ▼
  Cloudflare D1  (tee_times, sheet_freshness, runs)     schema.sql
            │  worker/index.js  (read API + freshness guard + prune cron)
            ▼
  Squarespace widget on oneteeapp.com   (frontend/*.js)
```

Write path top-down (scraping); read path bottom-up (a golfer's search). The
scrapers run on **GitHub Actions** on a schedule (§4); D1 + the Worker run on
**Cloudflare**. Cost target: ~$0/month on free tiers.

---

## 3. The scraping engine

**Adapters** (`scraper/adapters/`) — one class per platform behind a common
interface: `fetch(course, date) -> list[TeeTime]`. Rules that keep this
maintainable at scale:
- Adapters **never swallow errors** — the aggregator records them per course, so a
  broken platform can't silently poison the dataset.
- Parsing is **defensive** (platforms version-drift field names).
- Multi-course venues raise `PartialFetchError` so a partial failure publishes the
  sheets that answered while *shielding* the failed sheets' existing rows from
  deactivation (all-or-nothing would lose data either way).
- Shared HTTP session, retry/backoff, and the `TeeTime` factory live in `base.py`,
  not per adapter.

**Browser fetchers** (`scraper/browser_*.py`) — most platforms expose a hidden
JSON API an adapter can call directly (cheap, fast). Some don't, and need a real
browser. The reasons form a **block-type taxonomy** worth knowing, because the fix
differs:

| Block type | Symptom | Fix | Examples |
|---|---|---|---|
| Automation detection (Cloudflare managed challenge) | plain request 403s; real browser passes | stealth browser (**Patchright**) headful; no proxy needed | cps.golf |
| TLS-fingerprint block | plain 403; browser 200 | **curl_cffi** (impersonate Chrome TLS) — no full browser | golfrev |
| IP throttle / rate-limit | empty-200s or 429 from datacenter | **rotating datacenter proxy** (new IP per request) | teeitup/kenna |
| Per-IP concurrency 500s | 500s under parallel load, fine sequentially | **single-threaded** scrape (`--workers 1`) | teesnap |
| Client-only rendering (SPA/RSC) | data never appears in a fetchable response | drive a browser, parse the **rendered DOM** | clubcaddie, trutee, supersaas, golfwithaccess |
| reCAPTCHA-gated | priced endpoint needs a human token | mint token in a live browser (or use an open endpoint) | tenfore |

Patchright (a stealth Playwright fork) is the key unlock: it clears Cloudflare's
managed challenge from a free datacenter runner, so several "needs residential
proxy" platforms actually don't.

**The aggregator** (`aggregate.py`) maps `course["platform"]` → adapter via the
`ADAPTERS` dict, thread-pools `fetch` across courses (`--workers`), and writes one
JSON doc: `tee_times`, `courses_empty` (trustworthy clean-empties), and per-course
`errors`. `--shard i/N`, `--platforms`, `--states`, `--courses` narrow the run.

---

## 4. Scheduling (GitHub Actions)

**Tiers by recency.** Dates nearest today change most (bookings/cancellations), so
they're refreshed most:
- **near** (days 0–3): a self-chaining loop, effectively every few minutes.
- **mid** (days 3–7): hourly.
- **far** (days 8–30): every ~2 hours.

**Self-chaining loops.** Near tiers don't trust cron (GitHub's scheduler fires
late under load). Instead, at the end of a run, shard 0 re-dispatches the workflow
(`gh workflow run … --ref main`, guarded by `if: always() && matrix.shard == 0`);
cron is only a crash backstop. `concurrency: cancel-in-progress: false` makes a
successor queue behind the running loop instead of killing it.

**Sharding.** Near tiers fan out with a matrix (`shard: [0,1,2,3]`, `--shard i/4`)
so 4 jobs split the courses; per-host rate budgets divide by shard count too.

**Watchdog.** `watchdog-near-loops.yml` runs every 15 min and revives any loop
that died (e.g. a GitHub incident) — shrinking the dead-loop window from hours to
minutes.

**Per-platform workflows.** Platforms with their own engine/proxy needs get their
own scrapers (e.g. `scrape-teeitup-browser*`, `scrape-cps-browser*`,
`scrape-teesnap*`, `scrape-clubcaddie-browser`). Those platforms are `--exclude`d
from the generic tiers so the two never write the same course and clobber each
other. See `.github/workflows/README.md`.

---

## 5. Storage & the freshness model

**D1 tables** (`schema.sql`):
- `tee_times` — one row per open slot. PK `(course_slug, teetime, course_label)`
  (label in the key so a multi-course venue's simultaneous slots don't overwrite
  each other). Carries price, spots, holes, `booking_url`, `state`, `venue_id`,
  `source_role`, `active` (0 = gone), `first_seen_at`/`last_seen_at`.
- `sheet_freshness` — PK `(course_slug, date)`, one `last_ok_at` bumped on every
  *clean* scrape (rows OR a trustworthy empty), never on error. Cheap: O(courses),
  not O(slots).
- `runs` — per-scrape audit log (counts, errors).

**`d1.py` sync is a diff, not a rewrite.** It reads only the doc's courses' rows
for that date and INSERTs new / UPDATEs changed / deactivates vanished slots —
skipping errored or partially-errored courses so a transient failure never wipes
good data.

**Freshness guard (read path).** The Worker hides a slot unless its
`(course_slug, date)` was re-confirmed within a date-tier grace window (day 0 = 3h,
days 1–2 = 6h, days 3–7 = 18h, days 8–30 = 30h). So when a scraper stalls, its
rows stay in the table but silently drop off the live site instead of showing
phantom availability. A missing freshness row = shown (cold-start safe: hide only
on *proven* staleness).

**Elapsed pruning.** Booking sites keep showing the whole day's grid, so past slots
are deactivated by the Worker's own 5-minute cron (`worker/wrangler.toml`
`[triggers]` → `scheduled()`), per state timezone. Kept on Cloudflare because
GitHub's scheduler proved too unreliable.

> **Gotcha:** `last_seen_at` only bumps when price/spots *change*, so it can read
> "old" even when scraping is healthy (a stable tee sheet is a no-op upsert). Judge
> scraper health by the run log's `wrote … (N tee times, M errors)` and the
> freshness ledger — **never** by `last_seen_at`.

---

## 6. Read API & frontend

`worker/index.js` serves `/api/tee-times` (filter by state/date/city/price/…),
`/api/courses` (one row per venue), `/api/health`, `/api/directory` (the full
course list, from a bundled file), and `/api/revalidate` (live click-time re-check
for plain, datacenter-reachable platforms). See `worker/README.md`.

The frontend (`frontend/*.js`) is embedded in the Squarespace `/tee-times` widget:
`state-filter.js` builds the state dropdown from live data; `directory-cards.js`
shows "book by phone / private" cards for courses we can't book online.

---

## 7. Failure modes & how to diagnose (read this)

The dominant failure mode is **a green scraper run that silently lands 0 rows.**
It has bitten the project repeatedly, each time with a *different* root cause:
- a scraper's **proxy secret went stale** (teeitup) — fix the secret, no code change;
- a platform **500s under concurrency** (teesnap) — drop to `--workers 1`;
- a platform **rewrote its site into a client-only SPA** (clubcaddie) — the old
  HTML parser matched nothing; render the DOM instead.

Because they all present identically (workflow succeeds, 0 rows), the diagnostic
recipe is:
1. Read the run log's `wrote … (N tee times, M errors)` line — is it capturing?
2. If capturing but the site looks stale, check the **freshness ledger**, not
   `last_seen_at`.
3. If 0 rows, reproduce one course locally (`--platforms X --courses Y -v`), and
   for browser platforms inspect the live site to see if the flow/markup changed.

The systemic fix (built 2026-08-10): a **"landed 0 → alert"** check —
`python -m scraper.d1 coverage` reports per-platform freshness coverage from the
`sheet_freshness` ledger, and `.github/workflows/alert-landed-zero.yml` runs it
every 6h with `--alert`, so a whole platform going dark turns the run red and
GitHub emails the admins (within 6h, not weeks). The `probe_*` scripts/workflows
then run the one-course reproductions to find the specific cause.

---

## 8. Registry & discovery

`registry.json` is the crown jewel — perishable tee times vs. accumulated
knowledge of who-uses-what. It's regenerated from the per-state CSVs by
`build_registry.py`, which regex-extracts each platform's IDs from the booking
URL (the URL shape identifies the platform ~90% of the time). Some platforms need
IDs harvested from the booking page (static HTML, or a browser capturing the SPA's
own API calls). Re-run discovery periodically and when a course's error rate
spikes — that's the churn detector.

---

## 9. Legal & relationship strategy (read before scaling)

Factual context, not legal advice — consult a lawyer before commercial launch.
- Facts (course, time, price, availability) are generally not copyrightable, and
  US case law (*hiQ v. LinkedIn*) has been relatively scraping-favorable for
  publicly accessible data — but ToS, CFAA edge cases, and bot-protection
  circumvention are live issues.
- Where a platform actively bot-protects (GolfNow/EZLinks), prefer their **partner/
  affiliate program** over fighting it.
- Sanctioned paths exist and are better long-term: Lightspeed/Chronogolf Partner
  API, GolfNow affiliate, direct course deals. The durable business is
  *distribution partner*, not *scraper*; scraping is the bootstrap and gap-filler.
- Never resell stale prices as current; always deep-link to the platform's booking
  page for the actual transaction (every `TeeTime` carries a `booking_url`).

---

## 10. Scaling

Adding a state is mostly: curate its `*_golf_courses_booking.csv`, run
`build_registry.py`, and let the existing adapters cover it. New code is only
needed for a platform we haven't seen. Prioritize states by golfer demand. See
`docs/SCALING.md` for the cost/throughput math at 10k+ users (short version:
scraping cost is fixed by courses×dates×cadence and is decoupled from user count —
users read the cheap Worker+D1, not the source).
