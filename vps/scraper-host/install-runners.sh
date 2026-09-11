#!/usr/bin/env bash
# Install N self-hosted GitHub Actions runner instances as systemd services.
#
#   sudo -u scraper bash install-runners.sh <REGISTRATION_TOKEN> [COUNT]
#
# COUNT defaults to 12: scrape-far runs 6 shards and golfnow 6, and the hourly
# browser jobs overlap them, so fewer than ~12 re-creates the queueing this move
# exists to end. Each instance handles one job at a time and carries the label
# "scraper", which is what the moved workflows select on.
#
# Runners run as the unprivileged `scraper` user. They are NOT ephemeral on
# purpose: a persistent runner keeps the Playwright browser cache and the venv
# warm between jobs, which is most of the speed-up over hosted runners. The
# public-repo safety story rests on GitHub's "require approval for all outside
# collaborators" setting plus the absence of pull_request triggers on any
# self-hosted workflow — see the scale plan, Part III 8.5.
set -euo pipefail
TOKEN="${1:?registration token required}"
COUNT="${2:-12}"
REPO_URL="https://github.com/OneTeeApp/onetee-scraper"
BASE=/opt/onetee/runners
VER="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')"
TARBALL="actions-runner-linux-x64-${VER}.tar.gz"

mkdir -p "$BASE" && cd "$BASE"
[ -f "$TARBALL" ] || curl -fsSLo "$TARBALL" "https://github.com/actions/runner/releases/download/v${VER}/${TARBALL}"

for i in $(seq 1 "$COUNT"); do
  dir="$BASE/r$i"
  if [ -f "$dir/.runner" ]; then echo "r$i already configured, skipping"; continue; fi
  mkdir -p "$dir" && tar -xzf "$TARBALL" -C "$dir"
  # every job on this host sees the shared browser cache and the proxy/ingest
  # secrets from /etc/onetee/scraper.env (GitHub secrets still override).
  cat > "$dir/.env" <<ENV
PLAYWRIGHT_BROWSERS_PATH=/opt/onetee/pw-browsers
PATH=/opt/onetee/venv/bin:/usr/local/bin:/usr/bin:/bin
ENV
  ( cd "$dir" && ./config.sh --unattended --url "$REPO_URL" --token "$TOKEN" \
      --name "$(hostname -s)-r$i" --labels scraper --work _work --replace )
  # svc.sh needs root to write the unit; we are the scraper user, so print the
  # commands for root instead of sudo-ing inside a user shell.
  echo "SYSTEMD: sudo bash -c 'cd $dir && ./svc.sh install scraper && ./svc.sh start'"
done

cat <<'NOTE'

Now, as root, run each SYSTEMD line printed above (or:  for d in /opt/onetee/runners/r*; do (cd $d && ./svc.sh install scraper && ./svc.sh start); done ).
Verify: GitHub -> Settings -> Actions -> Runners should list them all as Idle.
NOTE
