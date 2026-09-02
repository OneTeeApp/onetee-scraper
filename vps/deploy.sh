#!/usr/bin/env bash
# OneTee VPS deploy — idempotent. Run by the deploy-vps GitHub workflow over SSH.
#   Phase 1: Postgres schema + indexes
#   Phase 2: Node read+ingest API (systemd service onetee-api on 127.0.0.1:8080)
#   Phase 3: one-time seed from the live worker (only if tee_times is empty)
#   Phase 4: Caddy HTTPS reverse proxy (vps.oneteeapp.com -> 127.0.0.1:8080)
#            + static file host for the indexable tee-time pages
#   Phase 6: indexable tee-time pages (pages/build.mjs) on a 2-hour timer
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
API_HOST="api.oneteeapp.com"
PAGES_HOST="tee-times.oneteeapp.com"
PAGES_ROOT="/var/www/onetee-pages"
echo "== OneTee VPS deploy =="
cd "$(dirname "$0")"

# ---------- Phase 1: schema ----------
sudo -u postgres psql -d onetee -c \
  "ALTER SCHEMA public OWNER TO onetee; GRANT ALL ON SCHEMA public TO onetee;" >/dev/null
export PGPASSWORD="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
psql -h 127.0.0.1 -U onetee -d onetee -v ON_ERROR_STOP=1 -f schema.sql
if [ -f accounts-schema.sql ]; then
  psql -h 127.0.0.1 -U onetee -d onetee -v ON_ERROR_STOP=1 -f accounts-schema.sql
  echo "accounts schema applied."
fi
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

# ---------- Phase 2b: /exec self-test — gate query shapes (numbered ?N + accounts) ----------
# systemd reports the unit "active" the moment node starts, before it listens;
# the last two deploys died here with curl rc=7 (connection refused) under
# set -e. Wait for /api/health first, and never let the self-test abort a deploy.
wait_for_api() {
  for i in $(seq 1 20); do
    if curl -s -o /dev/null http://127.0.0.1:8080/api/health; then echo "API listening (try $i)"; return 0; fi
    sleep 2
  done
  echo "WARN: API not listening on 8080 after 40s"; return 1
}
wait_for_api || true
ING_TOK="$(grep '^INGEST_TOKEN=' /root/onetee-api.env | cut -d= -f2)"
echo "---- /exec self-test (gate account query) ----"
curl -s -X POST http://127.0.0.1:8080/exec \
  -H "Authorization: Bearer ${ING_TOK}" -H 'Content-Type: application/json' \
  --data '{"sql":"SELECT count(*) AS n FROM users WHERE tier = ?1","params":["free"]}' \
  || echo "WARN: /exec self-test could not connect (rc=$?)"; echo

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
mkdir -p "${PAGES_ROOT}"
# Two sites: the API proxy, and the pre-rendered tee-time pages as plain files
# (Phase 6 builds them). Caddy provisions each certificate independently, so
# the pages host failing ACME (e.g. DNS not pointed yet) never affects the API.
cat > /etc/caddy/Caddyfile.new <<CADDY
${API_HOST} {
	reverse_proxy 127.0.0.1:8080
}

${PAGES_HOST} {
	root * ${PAGES_ROOT}
	encode gzip
	header Cache-Control "public, max-age=600"
	handle_errors {
		@404 {
			expression {http.error.status_code} == 404
		}
		rewrite @404 /404.html
		file_server
	}
	file_server
}
CADDY
# Never replace a working Caddyfile with one Caddy rejects: the API host lives here too.
if caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile >/tmp/caddy-validate.log 2>&1; then
  mv -f /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
  echo "Caddyfile valid (api + pages hosts)"
else
  echo "WARN: new Caddyfile rejected, keeping the current one:"; tail -5 /tmp/caddy-validate.log
  rm -f /etc/caddy/Caddyfile.new
fi
ufw allow 80/tcp  >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 2
echo "caddy active: $(systemctl is-active caddy) for ${API_HOST} (TLS provisions on first hit)"

# ---------- Phase 6: indexable tee-time pages ----------
# pages/build.mjs reads today's tee times from the local API + the directory
# bundle and writes ${PAGES_ROOT} (atomic swap). A systemd timer rebuilds every
# two hours; one build runs now so the host is never empty after a deploy.
mkdir -p /opt/onetee-pages
cp -f pages/build.mjs /opt/onetee-pages/
cat > /etc/systemd/system/onetee-pages.service <<UNIT
[Unit]
Description=OneTee static tee-time pages build (${PAGES_HOST})
After=network.target onetee-api.service
[Service]
Type=oneshot
Environment=API_BASE=http://127.0.0.1:8080
Environment=PAGES_OUT=${PAGES_ROOT}
Environment=PAGES_HOST=${PAGES_HOST}
Environment=DIRECTORY_PATH=/opt/onetee-api/directory.json
ExecStart=/usr/bin/node /opt/onetee-pages/build.mjs
UNIT
cat > /etc/systemd/system/onetee-pages.timer <<'UNIT'
[Unit]
Description=Rebuild OneTee tee-time pages every 2 hours
[Timer]
OnBootSec=3min
OnCalendar=*-*-* 00/2:07:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable onetee-pages.timer >/dev/null 2>&1 || true
systemctl restart onetee-pages.timer
echo "---- first pages build ----"
wait_for_api || true
if systemctl start onetee-pages.service; then
  journalctl -u onetee-pages.service -n 3 --no-pager -o cat || true
  echo "pages: $(find "${PAGES_ROOT}" -name index.html 2>/dev/null | wc -l) html files; $(cat "${PAGES_ROOT}/_build.json" 2>/dev/null | tr -d '\n' | head -c 300)"
else
  echo "WARN: pages build failed (see: journalctl -u onetee-pages.service)"; journalctl -u onetee-pages.service -n 20 --no-pager -o cat || true
fi
echo "pages timer: $(systemctl is-active onetee-pages.timer); next: $(systemctl show onetee-pages.timer -p NextElapseUSecRealtime --value)"
echo "---- pages host (localhost, Host header) ----"
curl -s -o /dev/null -w 'HTTP %{http_code} for http://127.0.0.1/ with Host ${PAGES_HOST}\n' -H "Host: ${PAGES_HOST}" http://127.0.0.1/ || true

# ---------- Phase 5: nightly pg_dump backup (local + offsite R2) ----------
# On-box dump protects against Postgres corruption / bad writes; the R2 copy is
# the offsite disaster copy. R2 upload is enabled only when R2 creds are supplied
# (R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY workflow secrets); local backups run
# regardless. This offsite copy MUST exist before D1 is ever deleted.
mkdir -p /root/backups

# Offsite (Cloudflare R2) credentials, written from workflow secrets if present.
if [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ]; then
  cat > /root/onetee-r2.env <<R2E
AWS_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
AWS_DEFAULT_REGION=auto
R2_BUCKET=${R2_BUCKET:-onetee-backups}
R2_ENDPOINT=${R2_ENDPOINT:-https://1b380acad1c1fd73ecf14983e7bc3a4c.r2.cloudflarestorage.com}
R2E
  chmod 600 /root/onetee-r2.env
  if ! command -v aws >/dev/null 2>&1; then
    echo "installing awscli..."
    apt-get update -qq || true
    if apt-get install -y awscli >/dev/null 2>&1; then :
    elif apt-get install -y python3-pip >/dev/null 2>&1 && pip3 install --break-system-packages awscli >/dev/null 2>&1; then :
    else echo "WARN: awscli install failed"; fi
  fi
  if command -v aws >/dev/null 2>&1; then
    echo "R2 offsite backup configured (bucket ${R2_BUCKET:-onetee-backups}); $(aws --version 2>&1 | head -1)"
  else
    echo "WARN: aws CLI not available — R2 upload will be skipped this run"
  fi
else
  echo "R2 creds not supplied — offsite upload disabled (local backups only)"
fi

cat > /root/onetee-backup.sh <<'BK'
#!/usr/bin/env bash
set -euo pipefail
BK=/root/backups
mkdir -p "$BK"
export PGPASSWORD="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
TS=$(date +%Y%m%d-%H%M%S)
DUMP="$BK/onetee-$TS.sql.gz"
pg_dump -h 127.0.0.1 -U onetee -d onetee | gzip > "$DUMP"
gzip -t "$DUMP"                                  # integrity check
# retain the 14 most recent LOCAL dumps
ls -1t "$BK"/onetee-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
# offsite copy to Cloudflare R2 (if configured)
if [ -f /root/onetee-r2.env ]; then
  set -a; . /root/onetee-r2.env; set +a
  # R2 rejects aws-cli's default streaming checksums; only send when required.
  export AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
  export AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
  if aws s3 cp "$DUMP" "s3://${R2_BUCKET}/onetee/onetee-${TS}.sql.gz" \
       --endpoint-url "$R2_ENDPOINT" --only-show-errors; then
    echo "R2 upload OK: onetee/onetee-${TS}.sql.gz"
  else
    echo "R2 upload FAILED for onetee-${TS}.sql.gz" >&2
    exit 3
  fi
fi
BK
chmod 700 /root/onetee-backup.sh
cat > /etc/systemd/system/onetee-backup.service <<'UNIT'
[Unit]
Description=OneTee Postgres pg_dump backup
[Service]
Type=oneshot
ExecStart=/root/onetee-backup.sh
UNIT
cat > /etc/systemd/system/onetee-backup.timer <<'UNIT'
[Unit]
Description=Nightly OneTee Postgres backup
[Timer]
OnCalendar=*-*-* 08:15:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable onetee-backup.timer >/dev/null 2>&1 || true
systemctl start onetee-backup.timer
# take one snapshot now, but under systemd, not this SSH session: pg_dump of
# 2M+ rows runs for minutes with no output, and deploy #28 died when the SSH
# connection dropped mid-dump ("client_loop: send disconnect"). --no-block
# returns immediately; the result lands in journalctl -u onetee-backup.
systemctl start --no-block onetee-backup.service && echo "backup snapshot started in the background (journalctl -u onetee-backup)"
echo "latest local dump: $(ls -1t /root/backups/onetee-*.sql.gz 2>/dev/null | head -1 || echo none)"

# ---------- Phase 5b: one-time restore verification (pull FROM R2, load it) ----------
# Proves the offsite copy is a real, restorable backup — not just bytes in a
# bucket. Downloads the newest R2 object, restores into a scratch DB, counts
# rows, drops it. Self-disables via a marker so it runs once.
cat > /root/onetee-restore-verify.sh <<'RV'
#!/usr/bin/env bash
set -uo pipefail
if [ ! -f /root/.onetee-restore-verified ] && [ -f /root/onetee-r2.env ] && command -v aws >/dev/null 2>&1; then
  ( set -a; . /root/onetee-r2.env; set +a
    export AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
    KEY="$(aws s3 ls "s3://${R2_BUCKET}/onetee/" --endpoint-url "$R2_ENDPOINT" 2>/dev/null | sort | tail -1 | awk '{print $NF}')"
    if [ -n "$KEY" ]; then
      echo "restore-verify: pulling s3://${R2_BUCKET}/onetee/${KEY} back from R2..."
      if aws s3 cp "s3://${R2_BUCKET}/onetee/${KEY}" /tmp/onetee-verify.sql.gz --endpoint-url "$R2_ENDPOINT" --only-show-errors \
         && gzip -t /tmp/onetee-verify.sql.gz; then
        sudo -u postgres psql -c "DROP DATABASE IF EXISTS onetee_verify;" >/dev/null 2>&1 || true
        sudo -u postgres psql -c "CREATE DATABASE onetee_verify OWNER onetee;" >/dev/null 2>&1 || true
        export PGPASSWORD="$(grep '^DB_PASSWORD=' /root/onetee-db.env | cut -d= -f2)"
        if gunzip -c /tmp/onetee-verify.sql.gz | psql -h 127.0.0.1 -U onetee -d onetee_verify -q >/dev/null 2>&1; then
          CNT="$(psql -h 127.0.0.1 -U onetee -d onetee_verify -tAc 'SELECT count(*) FROM tee_times' 2>/dev/null | tr -d '[:space:]')"
          echo "restore-verify OK: R2 copy restored cleanly — ${CNT} tee_times rows"
          touch /root/.onetee-restore-verified
        else
          echo "restore-verify FAILED: dump did not load" >&2
        fi
        sudo -u postgres psql -c "DROP DATABASE IF EXISTS onetee_verify;" >/dev/null 2>&1 || true
        rm -f /tmp/onetee-verify.sql.gz
      else
        echo "restore-verify: could not download/verify R2 object" >&2
      fi
    fi
  )
fi
RV
chmod 700 /root/onetee-restore-verify.sh
# Detached from the SSH session (it restores a multi-GB dump into a scratch DB);
# result in /root/onetee-restore-verify.log. Self-disables via the marker file.
if [ ! -f /root/.onetee-restore-verified ]; then
  setsid nohup /root/onetee-restore-verify.sh > /root/onetee-restore-verify.log 2>&1 < /dev/null &
  echo "restore-verify running in the background (log: /root/onetee-restore-verify.log)"
else
  echo "restore-verify: already verified (marker present)"
fi

echo "---- API /api/health (localhost) ----"; curl -s http://127.0.0.1:8080/api/health; echo
echo "== deploy done OK =="
