-- OneTee Postgres schema (ported from the D1 SQLite schema).
-- Idempotent: safe to re-run. Applied by vps/deploy.sh via the deploy-vps workflow.

CREATE TABLE IF NOT EXISTS tee_times (
  course_slug   TEXT NOT NULL,
  teetime       TEXT NOT NULL,
  course_label  TEXT NOT NULL DEFAULT '',
  course_name   TEXT NOT NULL,
  city          TEXT,
  state         TEXT,
  venue_id      TEXT,
  source_role   TEXT DEFAULT 'primary',
  platform      TEXT,
  holes         TEXT,
  open_spots    INTEGER,
  price_min     DOUBLE PRECISION,
  price_max     DOUBLE PRECISION,
  currency      TEXT DEFAULT 'USD',
  booking_url   TEXT,
  simulated     INTEGER DEFAULT 0,
  active        INTEGER DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  became_active_at TEXT,                -- set when a row returns from active=0
                                        -- (a cancellation). NULL if never booked.
  PRIMARY KEY (course_slug, teetime, course_label)
);

-- Forward migration for EXISTING databases. CREATE TABLE IF NOT EXISTS above
-- does nothing when the table already exists, and the /exec shim in
-- vps/api/server.mjs deliberately SKIPS all DDL, so scraper/d1.py's ALTER list
-- never reaches Postgres. This file, applied by vps/deploy.sh with psql, is the
-- ONLY path by which a new column reaches the live database.
-- IF NOT EXISTS is required: deploy.sh runs with ON_ERROR_STOP=1, so a plain
-- ADD COLUMN would abort the whole deploy on the second run.
ALTER TABLE tee_times ADD COLUMN IF NOT EXISTS became_active_at TEXT;

-- The index that turns a "state + date" lookup into a range read instead of a
-- full-table scan (the thing that ran up the D1 bill).
CREATE INDEX IF NOT EXISTS idx_tt_state_date ON tee_times (state, substr(teetime,1,10), active);
CREATE INDEX IF NOT EXISTS idx_tt_venue_date ON tee_times (venue_id, substr(teetime,1,10), active);
CREATE INDEX IF NOT EXISTS idx_tt_date       ON tee_times (substr(teetime,1,10));
CREATE INDEX IF NOT EXISTS idx_tt_course     ON tee_times (course_slug);
CREATE INDEX IF NOT EXISTS idx_tt_last_seen  ON tee_times (last_seen_at);
-- Partial index for the hot "active rows by time" paths: the hourly prune
-- (active = 1 AND state IN (...) AND teetime < local-now) and any scan that
-- filters active = 1 first. Only ~active rows are indexed, so it stays small
-- even though deactivated rows accumulate in the heap.
CREATE INDEX IF NOT EXISTS idx_tt_active_teetime ON tee_times (teetime) WHERE active = 1;
-- The prune's own predicate is `active = 1 AND state IN (...) AND teetime < now`,
-- and the index above cannot serve it: active rows are ~6% of the table and
-- nearly all in the future, but Postgres assumes `teetime < now` is independent
-- of `active` and so expects most active rows to match — and picks a parallel
-- seq scan of the whole heap instead (measured on a 7M-row copy: 173,000 buffers
-- per COUNT, six COUNTs per prune run; with this index, 385). That scan is what
-- ran into /exec's 25 s statement_timeout and failed `Prune elapsed tee times`
-- from 2026-08-22 on. Leading with `state` also serves every "active rows in
-- one state" read. INCLUDE (course_slug) is for the prune's second half, which
-- asks "which slugs have active rows" to find courses that left the registry:
-- with the slug in the index that is an index-only scan of the active rows
-- (8,452 buffers) instead of another pass over the heap (173,716).
-- CONCURRENTLY so the build never blocks ingest; psql -f runs
-- each statement in its own transaction, which CONCURRENTLY requires. A
-- concurrent build that dies leaves an INVALID index that IF NOT EXISTS would
-- then skip forever, so drop a broken one first.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
              WHERE c.relname = 'idx_tt_active_state_teetime' AND NOT i.indisvalid) THEN
    EXECUTE 'DROP INDEX idx_tt_active_state_teetime';
  END IF;
END $$;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tt_active_state_teetime ON tee_times (state, teetime) INCLUDE (course_slug) WHERE active = 1;

CREATE TABLE IF NOT EXISTS venue_geo (
  venue_id TEXT PRIMARY KEY,
  lat      DOUBLE PRECISION,
  lng      DOUBLE PRECISION,
  source   TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id               BIGSERIAL PRIMARY KEY,
  generated_at     TEXT NOT NULL,
  date             TEXT NOT NULL,
  courses_queried  INTEGER,
  courses_ok       INTEGER,
  tee_times        INTEGER,
  rows_inserted    INTEGER,
  rows_updated     INTEGER,
  rows_deactivated INTEGER,
  errors           TEXT
);

CREATE TABLE IF NOT EXISTS sheet_freshness (
  course_slug TEXT NOT NULL,
  date        TEXT NOT NULL,
  last_ok_at  TEXT NOT NULL,
  PRIMARY KEY (course_slug, date)
);

-- Page-view beacon from the static tee-time pages (tee-times.oneteeapp.com):
-- one row per view, no IP, no cookie; `sid` is a per-tab random id. Read by
-- /api/traffic for the private /_traffic/ page. Bots are flagged, not dropped.
CREATE TABLE IF NOT EXISTS page_hits (
  id     BIGSERIAL PRIMARY KEY,
  ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
  path   TEXT NOT NULL,
  ref    TEXT,
  sid    TEXT,
  mobile BOOLEAN,
  bot    BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_page_hits_ts ON page_hits (ts);
