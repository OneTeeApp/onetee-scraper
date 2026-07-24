-- Cloudflare D1 schema for the tee-time aggregator.
-- Applied automatically by `python -m scraper.d1 init` (idempotent).

CREATE TABLE IF NOT EXISTS tee_times (
  course_slug  TEXT NOT NULL,
  teetime      TEXT NOT NULL,            -- ISO local course time
  course_label TEXT NOT NULL DEFAULT '', -- sub-course within a multi-course
                                         -- facility (Hyland Hills Gold/Blue/Par 3);
                                         -- '' when the facility has one course
  course_name  TEXT NOT NULL,
  city         TEXT,
  state        TEXT,                     -- two-letter state (CO, AZ, ...) for frontend filtering
  venue_id     TEXT,                     -- stable physical-course id; groups the booking sources
                                         -- (native engine + GolfNow overflow, ...) that are one course
  source_role  TEXT DEFAULT 'primary',   -- 'primary' (native/only) | 'supplement' (extra inventory)
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
  -- course_label is in the PK: a 3-course facility (Hyland Hills) has three
  -- legitimate 7:00 slots; a 2-column key made them overwrite each other.
  PRIMARY KEY (course_slug, teetime, course_label)
);

-- Frontend reads: active slots for a state on a given day. The composite index
-- covers the common (state, date) filter; separate indexes cover course lookups
-- and active-only scans. At ~15k courses these keep reads sub-linear.
CREATE INDEX IF NOT EXISTS idx_teetimes_date   ON tee_times (substr(teetime, 1, 10));
CREATE INDEX IF NOT EXISTS idx_teetimes_active ON tee_times (active);
CREATE INDEX IF NOT EXISTS idx_teetimes_state_date
  ON tee_times (state, substr(teetime, 1, 10), active);
CREATE INDEX IF NOT EXISTS idx_teetimes_course ON tee_times (course_slug);
-- Group a physical course's sources together on a given day (native + supplement)
-- so the frontend can union + dedupe times per venue without a full scan.
CREATE INDEX IF NOT EXISTS idx_teetimes_venue
  ON tee_times (venue_id, substr(teetime, 1, 10), active);

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
