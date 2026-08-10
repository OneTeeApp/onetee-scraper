# `.github/workflows/` — the scheduled jobs

~73 GitHub Actions workflows run OneTee for free. They fall into three buckets:
**~17 production scrapers** (keep the site fresh), **~11 maintenance/deploy**, and
**~45 probes/diagnostics** (throwaway investigations — ignore for day-to-day work).

Concepts these rely on are in `docs/ARCHITECTURE.md` §4 and `docs/GLOSSARY.md`.

## Production scrapers (keep these healthy)

Refresh cadence follows recency — dates near today change most:
**near** = days 0–3, **mid** = days 3–7, **far** = days 8–30.

| Workflow | Window / cadence | Purpose |
|---|---|---|
| `scrape-near.yml` | near, self-chaining (cron backstop) | generic plain-HTTP near tier, 4-shard matrix |
| `scrape.yml` | mid, hourly | generic mid-window scrape |
| `scrape-far.yml` | far, every 2h | generic far-window scrape |
| `scrape-browser-near.yml` | near, self-chaining | browser-engine platforms, near window |
| `scrape-cps-browser.yml` | hourly + deep daily | CPS plain tenants hourly; Patchright deep daily |
| `scrape-cps-browser-near.yml` | near, self-chaining 30-min | CPS Cloudflare-challenged tenants |
| `scrape-teeitup-browser.yml` | far, every 2h | TeeItUp via rotating datacenter proxy |
| `scrape-teeitup-browser-near.yml` | near, self-chaining | TeeItUp near via proxy |
| `scrape-teesnap.yml` | near, self-chaining | Teesnap direct, **single-threaded** (concurrency 500s) |
| `scrape-teesnap-far.yml` | far, every 2h | Teesnap far window |
| `scrape-clubcaddie-browser.yml` | hourly + daily deep | Club Caddie SPA (rendered DOM) |
| `scrape-ezlinks-browser.yml` | hourly + daily deep | EZLinks |
| `scrape-golfnow-browser.yml` | hourly + daily deep | GolfNow |
| `scrape-golfwithaccess-browser.yml` | every 2h + daily deep | Golf With Access |
| `scrape-supersaas-browser.yml` | hourly + daily deep | SuperSaaS |
| `scrape-totale-browser.yml` | hourly | Total-e-Integrated |
| `scrape-trutee-browser.yml` | hourly + daily deep | Trutee |

Platforms with their own workflow are `--exclude`d from the generic tiers so the
two never write the same course and clobber each other in D1.

## Maintenance / deploy

| Workflow | Purpose |
|---|---|
| `deploy-worker.yml` | Deploy the Cloudflare Worker API |
| `prune-past.yml` | Hourly backstop to deactivate elapsed slots (Worker cron is primary) |
| `monitor-inventory.yml` | Hourly inventory-health witness |
| `probe-staleness.yml` | Scheduled sweep of stale/orphaned inventory (`--deactivate`) |
| `watchdog-near-loops.yml` | Every 15 min: revive any dead self-chaining loop |
| `directory.yml` | Build + verify the course directory (daily + push) |
| `rebuild-registry.yml` | Rebuild `registry.json` from the CSVs |
| `enrich-phones.yml` | Monthly: enrich directory phone numbers |
| `migrate.yml` | D1 schema/backfill migrations |
| `state-status.yml` | Daily per-state coverage report |
| `snapshot-gate.yml` | Snapshot the Cloudflare gate config |

## Probes / diagnostics (throwaway)

`workflow_dispatch`-triggered investigation runs, grouped by topic:
- **CPS (~11):** `probe-cps-*`, `probe-noproxy`, `probe-proxy-diag`,
  `probe-residential-diag` — proxy / Cloudflare-clearing experiments.
- **TeeItUp/kenna (~6):** `probe-teeitup-*`, `diag-kenna-*`, `probe-kenna-impersonate`.
- **Teesnap (~3):** `diag-teesnap*`, `probe-teesnap-methods`.
- **GolfNow (~3):** `diag-golfnow`, `crosscheck-golfnow`, `flap-golfnow`.
- **Colorado/frontend (~3):** `co-frontend-probe`, `co-regression`, `co-verify-empties`.
- **Misc (~19):** new-engine probes (`probe-golfrev`, `probe-gwa`, `probe-tenfore`,
  `probe-webshare`), lead verification (`probe-needs-ids`, `probe-newly-ready`,
  `probe-open-leads`), and single-course/pipeline diagnostics.

Keep these for reference, but they aren't part of the running system.

## Recurring patterns

- **Self-chaining loop** — near tiers re-dispatch themselves at the end of a run
  (`gh workflow run … --ref main`, guarded by `if: always() && matrix.shard == 0`);
  cron is only a crash backstop, because GitHub's scheduler fires late under load.
  Requires `permissions: actions: write`.
- **Concurrency group** — `concurrency: { group, cancel-in-progress: false }` so a
  chained successor **queues** behind the running loop instead of killing it.
- **Matrix sharding** — `matrix: shard: [0,1,2,3]` + `--shard i/4` splits the
  courses across parallel jobs; only shard 0 re-dispatches.
- **Watchdog** — `watchdog-near-loops.yml` restarts loops that die (e.g. a GitHub
  incident), shrinking the dead window from hours to ~15 min.

## Deploying a workflow change

There is no `git push` from the automation environment; workflow/scraper files are
committed via GitHub's web upload UI (Add file → Upload files) to the relevant
directory. A push to `main` that touches a scraped file auto-triggers that
platform's workflow (see each file's `on: push: paths:`).
