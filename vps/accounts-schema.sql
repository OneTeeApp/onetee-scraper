-- OneTee gate accounts schema — migrated from the onetee-gate Worker's D1 tables.
-- Identity stays in Clerk and payment truth in Stripe; these are the gate's own
-- derived tables (tier mirror, webhook dedup log, saved searches). Idempotent;
-- applied by vps/deploy.sh after schema.sql.

CREATE TABLE IF NOT EXISTS users (
  id                     TEXT PRIMARY KEY,   -- Clerk user id
  email                  TEXT,
  created_at             TEXT NOT NULL,
  last_seen_at           TEXT NOT NULL,
  tier                   TEXT NOT NULL DEFAULT 'free',
  stripe_customer_id     TEXT,
  stripe_subscription_id TEXT,
  subscription_status    TEXT,
  current_period_end     TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users (stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_users_email           ON users (email);

CREATE TABLE IF NOT EXISTS billing_events (
  id          TEXT PRIMARY KEY,              -- Stripe event id (idempotency)
  type        TEXT NOT NULL,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
  id         BIGSERIAL PRIMARY KEY,          -- was SQLite INTEGER AUTOINCREMENT
  user_id    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  label      TEXT NOT NULL,
  criteria   TEXT NOT NULL,                  -- JSON blob
  results    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_searches_user ON searches (user_id, id DESC);

-- Phase 1 saved searches (2026-08-12): promote a Recent search to a permanent
-- Saved one by starring it. saved = 0/1 star flag; name = user-editable
-- label. Both idempotent so re-running the deploy is a no-op after the first.
ALTER TABLE searches ADD COLUMN IF NOT EXISTS saved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE searches ADD COLUMN IF NOT EXISTS name  TEXT;
-- Saved-only lookups (the Tee Times chips + the account Saved list) as a range read.
CREATE INDEX IF NOT EXISTS idx_searches_saved ON searches (user_id, saved, id DESC);

-- ---------------------------------------------------------------------------
-- Tee-time alerts (2026-08-13)
--
-- Opt-in and narrow: a member names explicit courses plus a time window and a
-- day. NOTHING is ever emailed without a row here. Premium-only is enforced in
-- the gate, not by this table.
CREATE TABLE IF NOT EXISTS alerts (
  id         BIGSERIAL PRIMARY KEY,
  user_id    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  name       TEXT,                        -- user-editable label
  criteria   TEXT NOT NULL,               -- JSON: courses[], date|weekday, todLo,
                                          -- todHi, players, holes
  active     INTEGER NOT NULL DEFAULT 1,  -- paused rather than deleted
  -- Two watermarks, not one. Cancellations send instantly and new releases are
  -- batched, so the two paths advance independently and must not clobber each
  -- other. Compared against tee_times.became_active_at / .first_seen_at.
  last_cancel_at TEXT,
  last_new_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_user   ON alerts (user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (active, id) WHERE active = 1;

-- One row per (alert, slot) actually emailed. This is the belt to the
-- watermarks' braces, and it is what stops the double-send: a slot released,
-- booked and cancelled inside a day has BOTH first_seen_at and
-- became_active_at recent (measured: 67 of 12,000 live rows, tightest gap 11
-- minutes), so it matches the cancellation branch AND the new-release branch.
-- The instant path writes its key here; the batch path skips keys already
-- present. Safe across restarts and re-runs in a way a watermark alone is not.
CREATE TABLE IF NOT EXISTS alert_sends (
  alert_id BIGINT NOT NULL,
  slot_key TEXT   NOT NULL,   -- course_slug|teetime|course_label
  sent_at  TEXT   NOT NULL,
  reason   TEXT   NOT NULL,   -- 'cancellation' | 'new'
  PRIMARY KEY (alert_id, slot_key)
);
-- Pruning old send history without a full scan.
CREATE INDEX IF NOT EXISTS idx_alert_sends_at ON alert_sends (sent_at);
