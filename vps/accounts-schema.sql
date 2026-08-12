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
