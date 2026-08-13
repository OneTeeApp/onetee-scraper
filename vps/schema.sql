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
