#!/usr/bin/env bash
# OneTee scraper host — one-shot, idempotent bootstrap.
#
# Turns a fresh Ubuntu 24.04 box (OVH RISE-S or similar) into a self-hosted
# GitHub Actions runner host for the scrape workflows, hardened the way the
# scale plan (Part II, Routes 2–3) describes. Safe to re-run; every step checks
# before it changes anything.
#
#   ssh root@<new-box>   (or ubuntu@ with sudo)
#   curl -fsSLO https://raw.githubusercontent.com/OneTeeApp/onetee-scraper/main/vps/scraper-host/bootstrap.sh
#   sudo bash bootstrap.sh
#
# What it does, in order:
#   1. users: `onetee` (admin, sudo) and `scraper` (service account, no sudo)
#   2. SSH hardening: keys only, no root login
#   3. Tailscale installed (you run `tailscale up --ssh` yourself — it prints a
#      login URL that has to be approved in YOUR admin console)
#   4. ufw: default deny inbound, allow only the tailscale interface
#      (22/tcp stays open until you confirm Tailscale SSH works; see the end)
#   5. unattended security upgrades, fail2ban
#   6. runtime: python3.12 venv, git, xvfb, the Chromium system libraries
#      Playwright needs — so workflows never need `sudo apt` per run
#   7. a shared Playwright browser cache at /opt/onetee/pw-browsers so each job
#      does not re-download Chromium
#   8. /etc/onetee/ for secrets (root:scraper 0640, empty — you fill it)
#
# It does NOT register the runners (that needs a short-lived token from the
# GitHub UI — see install-runners.sh) and does NOT install Postgres; the
# runner-based phase keeps all data on the API VPS.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo bash bootstrap.sh)"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

log "1/8 users"
id -u onetee >/dev/null 2>&1 || adduser --disabled-password --gecos "OneTee admin" onetee
usermod -aG sudo onetee
id -u scraper >/dev/null 2>&1 || adduser --system --group --home /var/lib/onetee-scraper --shell /bin/bash scraper
# carry the root authorized_keys to the admin user so the first login works
if [ -f /root/.ssh/authorized_keys ] && [ ! -f /home/onetee/.ssh/authorized_keys ]; then
  install -d -m 700 -o onetee -g onetee /home/onetee/.ssh
  install -m 600 -o onetee -g onetee /root/.ssh/authorized_keys /home/onetee/.ssh/authorized_keys
fi

log "2/8 sshd hardening"
cat > /etc/ssh/sshd_config.d/10-onetee.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
CONF
systemctl reload ssh || systemctl reload sshd

log "3/8 tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

log "4/8 firewall (22 left open until Tailscale SSH is confirmed)"
apt-get install -y -q ufw >/dev/null
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow in on tailscale0 >/dev/null
ufw allow 22/tcp >/dev/null
ufw --force enable

log "5/8 unattended upgrades + fail2ban"
apt-get install -y -q unattended-upgrades fail2ban >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now fail2ban

log "6/8 runtime + browser system libraries"
apt-get update -q
apt-get install -y -q git curl jq xvfb python3.12 python3.12-venv python3-pip \
  libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 \
  libcairo2 libatspi2.0-0t64 fonts-liberation fonts-noto-color-emoji >/dev/null

log "7/8 shared Playwright browser cache"
install -d -m 755 -o scraper -g scraper /opt/onetee /opt/onetee/pw-browsers
sudo -u scraper bash -c '
  cd /opt/onetee
  [ -d venv ] || python3.12 -m venv venv
  ./venv/bin/pip install -q --upgrade pip playwright patchright
  PLAYWRIGHT_BROWSERS_PATH=/opt/onetee/pw-browsers ./venv/bin/python -m playwright install chromium
  PLAYWRIGHT_BROWSERS_PATH=/opt/onetee/pw-browsers ./venv/bin/python -m patchright install chromium || true
'

log "8/8 secrets directory"
install -d -m 750 -o root -g scraper /etc/onetee
[ -f /etc/onetee/scraper.env ] || { touch /etc/onetee/scraper.env; chmod 640 /etc/onetee/scraper.env; chown root:scraper /etc/onetee/scraper.env; }

cat <<'NEXT'

Bootstrap complete. Next, in this order:

  1. tailscale up --ssh          # prints a URL; approve the node in the admin console
  2. From YOUR machine:  ssh onetee@<tailscale-hostname>   # confirm this works
  3. Then close the public SSH port:  ufw delete allow 22/tcp
  4. Register the runners:  sudo -u scraper bash /opt/onetee/repo/vps/scraper-host/install-runners.sh <TOKEN> 12
     (token: GitHub -> Settings -> Actions -> Runners -> New self-hosted runner; short-lived)
  5. Merge the "move cron scrapes to self-hosted" PR. Watch Actions: jobs should
     show "self-hosted" as the runner and start within seconds.
NEXT
