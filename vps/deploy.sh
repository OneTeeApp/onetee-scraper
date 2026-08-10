#!/usr/bin/env bash
# OneTee VPS deploy — idempotent. Run by the deploy-vps GitHub workflow over SSH.
#   Phase 1: Postgres schema + indexes
#   Phase 2: Node read+ingest API (systemd service onetee-api on 127.0.0.1:8080)
#   Phase 3: one-time seed from the live worker (only if tee_times is empty)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "== OneTee VPS deploy =="
cd "$(dirname "$0")"

# ---------- Phase 1: schema ----------
sudo -u postgres psql -d onetee -c \
  "ALTER SCHEMA public OWNER TO onetee; GRANT ALL ON SCHEMA public TO onetee;" >/dev/null
export PGPASSWORD="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
psql -h 127.0.0.1 -U onetee -d onetee -v ON_ERROR_STOP=1 -f schema.sql
echo "schema applied."

# ---------- Phase 2: API service ----------
if ! command -v node >/dev/null 2>&1; then
  echo "installing node + npm..."
  apt-get update -qq
  apt-get install -y nodejs npm >/dev/null
fi
echo "node $(node --version)"

mkdir -p /opt/onetee-api
cp -f api/server.mjs api/package.json /opt/onetee-api/
if [ -f api/directory.json ]; then cp -f api/directory.json /opt/onetee-api/; else echo "WARN: no directory.json bundled"; fi
cp -f api/seed_from_worker.py /opt/onetee-api/
( cd /opt/onetee-api && npm install --omit=dev --no-audit --no-fund )

# Ingest token — generated once, stored only on the box.
if [ ! -f /root/onetee-api.env ]; then
  echo "INGEST_TOKEN=$(openssl rand -hex 24)" > /root/onetee-api.env
  chmod 600 /root/onetee-api.env
fi
DB_PW="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
ING_TOK="$(grep '^INGEST_TOKEN=' /root/onetee-api.env | cut -d= -f2)"

# Runtime env file (chmod 600) so the DB password stays out of the world-readable unit.
cat > /root/onetee-api-runtime.env <<ENVV
DATABASE_URL=postgresql://onetee:${DB_PW}@127.0.0.1:5432/onetee
INGEST_TOKEN=${ING_TOK}
DIRECTORY_PATH=/opt/onetee-api/directory.json
PORT=8080
ENVV
chmod 600 /root/onetee-api-runtime.env

cat > /etc/systemd/system/onetee-api.service <<'UNIT'
[Unit]
Description=OneTee read+ingest API
After=network.target postgresql.service
[Service]
EnvironmentFile=/root/onetee-api-runtime.env
ExecStart=/usr/bin/node /opt/onetee-api/server.mjs
Restart=always
RestartSec=3
User=root
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable onetee-api >/dev/null 2>&1 || true
systemctl restart onetee-api
sleep 3
echo "service active: $(systemctl is-active onetee-api)"

# ---------- Phase 3: seed (only if empty) ----------
CNT=$(psql -h 127.0.0.1 -U onetee -d onetee -tAc "SELECT count(*) FROM tee_times" | tr -d '[:space:]')
echo "tee_times rows before seed: $CNT"
if [ "$CNT" = "0" ]; then
  echo "seeding from live worker..."
  python3 api/seed_from_worker.py || echo "(seed failed, continuing)"
fi
unset PGPASSWORD

echo "---- API /api/health ----"; curl -s http://127.0.0.1:8080/api/health; echo
echo "---- API /api/tee-times?state=CO&limit=3 ----"
curl -s "http://127.0.0.1:8080/api/tee-times?state=CO&limit=3"; echo
echo "== deploy done OK =="
