-- Cloudflare D1 schema for the tee-time aggregator.
-- Applied automatically by `python -m scraper.d1 init` (idempotent).

CREATE TABLE IF NOT EXISTS tee_times (
  course_slug  TEXT NOT NULL,
  teetime      TEXT NOT NULL,            -- ISO local course time
  course_name  TEXT NOT NULL,
  city         TEXT,
  platform     TEXT,
  holes        TEXT,                     -- e.g. "18" or "9/18"
  open_spots   INTEGER,
  price_min    REAL,
  price_max    REAL,
  currency     TEXT DEFAULT 'USD',
  booking_url  TEXT,
  simulated    INTEGER DEFAULT 0,
  active       INTEGER DEFAULT 1,        -- 0 = slot disappeared (booked/closed)
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  PRIMARY KEY (course_slug, teetime)
);

CREATE INDEX IF NOT EXISTS idx_teetimes_date   ON tee_times (substr(teetime, 1, 10));
CREATE INDEX IF NOT EXISTS idx_teetimes_active ON tee_times (active);

CREATE TABLE IF NOT EXISTS runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_at    TEXT NOT NULL,
  date            TEXT NOT NULL,
  courses_queried INTEGER,
  courses_ok      INTEGER,
  tee_times       INTEGER,
  rows_inserted   INTEGER,
  rows_updated    INTEGER,
  rows_deactivated INTEGER,
  errors          TEXT                   -- JSON array of per-course errors
);
