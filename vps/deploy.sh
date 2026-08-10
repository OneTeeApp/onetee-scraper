#!/usr/bin/env bash
# OneTee VPS deploy — idempotent. Run by the deploy-vps GitHub workflow over SSH.
#   Phase 1: Postgres schema + indexes
#   Phase 2: Node read+ingest API (systemd service onetee-api on 127.0.0.1:8080)
#   Phase 3: one-time seed from the live worker (only if tee_times is empty)
#   Phase 4: Caddy HTTPS reverse proxy (vps.oneteeapp.com -> 127.0.0.1:8080)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
API_HOST="api.oneteeapp.com"
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

if [ -n "${VPS_INGEST_TOKEN:-}" ]; then
  # Shared token from the VPS_INGEST_TOKEN repo secret (so the scraper and the
  # API use the same key). Overwrites any previously-generated one.
  echo "INGEST_TOKEN=${VPS_INGEST_TOKEN}" > /root/onetee-api.env
  chmod 600 /root/onetee-api.env
elif [ ! -f /root/onetee-api.env ]; then
  echo "INGEST_TOKEN=$(openssl rand -hex 24)" > /root/onetee-api.env
  chmod 600 /root/onetee-api.env
fi
DB_PW="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
ING_TOK="$(grep '^INGEST_TOKEN=' /root/onetee-api.env | cut -d= -f2)"

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

# ---------- Phase 4: Caddy HTTPS reverse proxy ----------
if ! command -v caddy >/dev/null 2>&1; then
  echo "installing caddy..."
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y caddy >/dev/null
fi
cat > /etc/caddy/Caddyfile <<CADDY
${API_HOST} {
	reverse_proxy 127.0.0.1:8080
}
CADDY
ufw allow 80/tcp  >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 2
echo "caddy active: $(systemctl is-active caddy) for ${API_HOST} (TLS provisions on first hit)"

echo "---- API /api/health (localhost) ----"; curl -s http://127.0.0.1:8080/api/health; echo
echo "== deploy done OK =="
