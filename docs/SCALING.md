# Scaling to ~15,000 US courses

This documents the architecture decisions that let the aggregator grow from a
few hundred courses to national coverage (~15k) without a rewrite, and the
levers to pull as volume grows.

## Data model / D1

- **`state` column + indexes.** Every tee time carries a two-letter `state`
  (`CO`, `AZ`, …) sourced from the registry (`course.state` → `TeeTime.state` →
  D1). The frontend filters by state directly. Indexes:
  `idx_teetimes_state_date (state, day, active)` covers the primary
  "active slots in a state on a day" read; `idx_teetimes_course` covers
  per-course lookups; the existing date/active indexes remain.
- **Course-scoped sync (the key DB fix).** `d1.sync()` reads existing rows only
  for the courses in the current document (chunked `WHERE course_slug IN (...)`),
  not the whole date. Cost is O(shard size), not O(all courses in the DB), so a
  sync stays cheap whether the table holds 5k rows or 5M. Deactivation is
  already scoped to scraped courses, so this is behaviour-preserving.
- **Write budget.** D1's free tier allows 100k row *writes*/day; reads are cheap
  (5M/day). Diff-sync means steady-state writes ≈ only slots whose price/spots
  changed or that appeared/disappeared. Rough envelope at 15k courses × ~40
  slots × 3 days ≈ 1.8M active rows; the initial backfill and daily churn will
  exceed the free tier — **move D1 to a paid plan** (or batch via the D1
  `/batch` API to cut round-trips) before national rollout. Nothing in the code
  assumes the free tier; this is an account/billing switch.

## Scraping throughput — sharding

- **`--shard i/N`** (see `scraper/sharding.py`) selects a deterministic 1/N
  slice of courses by sorted slug (modulo N). Every course lands in exactly one
  shard, the split is stable across dates/reruns, and shards are balanced.
- **Actions matrix.** `scrape.yml` runs the slices as parallel matrix jobs
  (`shard: [0,1,2,3]`). Wall-clock stays roughly flat as the registry grows —
  to scale out, widen the matrix list (e.g. to `[0..15]`); no code change.
  Because the sync is course-scoped, parallel shards never contend in D1.
- **Browser fetchers** (`browser_cps/ezlinks/golfnow/clubcaddie`) take the same
  `--shard` flag. Their workflows can adopt the identical matrix when a single
  platform's course count outgrows one job.

## Rate limits under sharding

Shared-host APIs must stay under their limit across the *whole* fleet, not
per-shard. `scraper.sharding.set_env_shard_count()` publishes `SHARD_COUNT`, and
per-host throttles widen their cadence by that factor — e.g. the TeeItUp/kenna
throttle uses `gap = base_gap × SHARD_COUNT`, so 4 shards each pace at 1/4 the
rate and the aggregate cadence is constant no matter how many shards run. New
shared-host adapters should follow the same pattern. Per-tenant hosts (cps.golf,
chronogolf clubs) are naturally distributed and need no global coordination.

## Registry

- Per-state CSVs (`colorado_…csv`, `arizona_…csv`) are merged by
  `build_registry.py` into one `registry.json` with a `state` field and
  globally-unique slugs (slugs strip descriptive parentheticals so they stay
  stable across directory edits; collisions get a `-<state>` suffix). Adding a
  state = add a CSV to `SOURCES`. At 50 states this stays an O(n) build; if the
  single JSON ever gets unwieldy, shard the registry by state on disk — the
  loaders already filter by course, so no consumer assumes one file.

## Multi-source venues (native engine + GolfNow overflow)

A physical course can sell tee times through more than one channel — typically a
native engine (foreUP/EZLinks/TeeItUp/…) that holds the full sheet, plus a
GolfNow listing that carries only *overflow* inventory. GolfNow is almost never a
course's sole channel, so a "golfnow" tag usually means "we found the overflow
link, not the real one."

Model: every registry entry is one booking **source** and carries a `venue_id`
(the physical course) plus a `source_role` (`primary` = native/only, `supplement`
= overflow). The primary owns the clean venue slug; each supplement gets a
`-<platform>` slug. Why separate slugs instead of merging at write time: the two
sources are scraped by *different* workflows (plain vs. `browser_golfnow`) on
different cadences, and D1 sync is course-slug-scoped — one slug per source keeps
each sync independent and clobber-free. The union+dedupe happens at **read time**:
the frontend groups rows by `venue_id`, and on a `(venue_id, teetime)` collision
keeps the `primary` row (its native booking link). `idx_teetimes_venue` makes the
per-venue grouping cheap. This scales to any number of sources per venue without
changing the write path — add a source row sharing the `venue_id`.

Consumer requirement: the Worker API must expose `venue_id` + `source_role`, and
the frontend must group by `venue_id`; otherwise a course with a supplement shows
as two cards.

## What to do next as volume climbs

1. Widen the `scrape.yml` matrix (and adopt it in the browser workflows) as
   per-shard runtime approaches the job timeout.
2. Switch D1 to a paid plan (or the batch API) ahead of the write volume.
3. Capture correct TeeItUp aliases in bulk and resolve the Chronogolf-empty
   category (both are data/coverage work, not architecture).
