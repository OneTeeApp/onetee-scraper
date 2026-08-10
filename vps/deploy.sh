#!/usr/bin/env bash
# OneTee VPS deploy — idempotent. Run by the deploy-vps GitHub workflow over SSH.
# Phase 1: apply the Postgres schema + indexes. Later phases (API, service,
# Caddy) get appended here and re-run safely.
set -euo pipefail

echo "== OneTee VPS deploy =="
cd "$(dirname "$0")"

# The app user must own the public schema to create objects (Postgres 15+ locks
# this down by default). Harmless to re-run.
sudo -u postgres psql -d onetee -c \
  "ALTER SCHEMA public OWNER TO onetee; GRANT ALL ON SCHEMA public TO onetee;" >/dev/null

# Apply schema + indexes using the DB password stored only on this box.
export PGPASSWORD="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
psql -h 127.0.0.1 -U onetee -d onetee -v ON_ERROR_STOP=1 -f schema.sql

echo "---- tables ----"
psql -h 127.0.0.1 -U onetee -d onetee -c "\dt"
echo "---- tee_times indexes ----"
psql -h 127.0.0.1 -U onetee -d onetee -c "\d tee_times" | sed -n '/Indexes/,/^$/p'
unset PGPASSWORD

echo "== deploy done OK =="
