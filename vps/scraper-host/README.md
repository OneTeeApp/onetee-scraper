# Scraper host — runbook

The box that runs the scrape fleet. Phase 1 (this PR): self-hosted GitHub
Actions runners take the 12 cron scrape workflows off GitHub's fleet. Phase 2:
per-platform sweeper services replace the workflows one platform at a time,
organised as **state × platform** units (see
`claude/vps-runners-rollout-2026-09-02.md` in the project, §7).

## Day-one sequence

| # | Who | What |
|---|---|---|
| 1 | Brian | Order OVH RISE-S (Vint Hill or Hillsboro), Ubuntu 24.04, RAID-1, SSH public key at order time. |
| 2 | Brian | `curl -fsSLO https://raw.githubusercontent.com/OneTeeApp/onetee-scraper/main/vps/scraper-host/bootstrap.sh && sudo bash bootstrap.sh` (use the PR branch in the URL until merged). |
| 3 | Brian | `tailscale up --ssh` → approve node → confirm `ssh onetee@<tailscale-name>` from the laptop → `sudo ufw delete allow 22/tcp`. |
| 4 | Brian | Fill `/etc/onetee/scraper.env` from `scraper.env.example` (root:scraper 0640). Never paste values into chat. |
| 5 | Brian | GitHub → Settings → Actions → Runners → New self-hosted runner → copy the token. `sudo -u scraper bash install-runners.sh <TOKEN> 12`, then the printed `svc.sh` lines as root. |
| 6 | Brian | Runners page shows 12 × Idle with label `scraper` → merge PR #1. |
| 7 | Claude | Watch the first cron cycle of each tier (runner name `<host>-rN` on the job page). Missing system library → add to `bootstrap.sh`, never to a workflow. |
| 8 | Claude | After 24 h: re-run the scrape-times analysis; queue waits should be ~0. |

## Adding capacity

`sudo -u scraper bash install-runners.sh <TOKEN> 16` — configured runners are
skipped, new ones (`r13..r16`) are added. Then the printed `svc.sh` lines.
Budget: ~16–20 headful Chromium jobs on 64 GB; plain-HTTP jobs are light.

## Where things live

| Path | What |
|---|---|
| `/opt/onetee/venv` | shared Python venv (playwright, patchright) |
| `/opt/onetee/pw-browsers` | shared Playwright browser cache (`PLAYWRIGHT_BROWSERS_PATH`) |
| `/opt/onetee/runners/rN` | one actions-runner per directory, service `actions.runner.*` |
| `/etc/onetee/scraper.env` | secrets, root:scraper 0640, never in git |
| `/etc/ssh/sshd_config.d/10-onetee.conf` | keys only, no root login |

## Operating

```
sudo systemctl list-units 'actions.runner.*'          # runner services
sudo journalctl -u 'actions.runner.*' -S -1h          # last hour of runner logs
cd /opt/onetee/runners/r1 && sudo ./svc.sh status     # one runner
sudo tailscale status                                 # who can reach the box
sudo ufw status verbose                               # should show: deny incoming, allow on tailscale0
```

A runner that shows *Offline* on GitHub: `sudo ./svc.sh stop && sudo ./svc.sh start`
in its directory. A runner stuck on a job: cancel the job on GitHub first.

## Safety rules that apply on this box

Brian holds every credential. No secret is ever echoed into a transcript or a
log. No CAPTCHA solving; no account creation to scrape; logged-out only. Runners
run as the unprivileged `scraper` user with no sudo. The repository is public,
so the only thing standing between a fork PR and this box is the repo's
"Require approval for all external contributors" setting — do not change it,
and do not add `pull_request` triggers to any self-hosted workflow.
