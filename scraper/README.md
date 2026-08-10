# `scraper/` — the scraping engine

Python code that fetches tee times from booking platforms and writes them to the
database. Run from the repo root as modules (`python -m scraper.aggregate …`).

New to the project? Read the root `README.md` and `docs/ARCHITECTURE.md` §3 first.

## The pipeline, in order

1. **`dates.py`** decides which calendar dates to scrape (time-zone aware).
2. **`aggregate.py`** loops the registry, calls the right adapter per course, and
   writes one JSON doc of all results for a date.
3. **`d1.py`** syncs that doc into Cloudflare D1 (or a local SQLite copy).

```bash
python -m scraper.aggregate --date 2026-08-11            # scrape everything
python -m scraper.aggregate --date 2026-08-11 --platforms foreup --verbose
python -m scraper.d1 push --data output/tee_times.json --local   # into local SQLite
python -m scraper.d1 stats --local
python -m scraper.dates --registry registry.json --days 4        # list dates to scrape
```

## Core modules

| File | Role | Notes |
|---|---|---|
| `aggregate.py` | **Conductor** | `ADAPTERS` dict maps platform → adapter class; thread-pools `fetch` across courses; flags: `--date --platforms --exclude --courses --states --shard --workers --out`. |
| `d1.py` | **DB sync** | Diff-based (`sync()` inserts new / updates changed / deactivates gone); `prune_past()` drops elapsed slots; stamps `sheet_freshness`; backends: D1 REST or local SQLite (`--local`). CLI: `init/migrate/push/prune/stats`. |
| `dates.py` | **Which dates** | "Today" differs by timezone; unions today+offset across every timezone the registry covers, so evening slots aren't skipped. |
| `models.py` | **Data shapes** | `TeeTime` (the normalized slot) and `FetchResult`. Everything ends up as a `TeeTime`. |
| `sharding.py` | **Parallelism** | `--shard i/N` selects a deterministic slice of courses (sorted-slug modulo N); publishes `SHARD_COUNT` so per-host rate budgets divide by N. |
| `onetee.py` | **Optional client** | POSTs/CSV-exports the aggregate to a future ingest API; no-ops if `ONETEE_API_URL` unset. |
| `gen_sample.py` | **Fake data** | Seeded simulated tee times in the real schema, for demo/dev without live access. |

## `adapters/` — one file per platform

The plain-HTTP fetchers. Each subclasses `Adapter` and implements
`fetch(course, date) -> [TeeTime]`. See **`adapters/README.md`** for the full
platform table and how to add one.

## `browser_*.py` — browser-driven fetchers

For platforms where a plain request is blocked or the data only renders in a real
browser (see the block-type taxonomy in `docs/ARCHITECTURE.md` §3). Each drives a
(sometimes stealth/Patchright) Chromium and produces the same `TeeTime` output.

| File | Platform | Why a browser |
|---|---|---|
| `browser_cps.py` | Club Prophet (cps.golf) | Cloudflare managed challenge (Patchright clears it) |
| `browser_teeitup.py` | TeeItUp/kenna (far window) | datacenter IP throttle (proxy) |
| `browser_clubcaddie.py` | Club Caddie | client-only SPA — parse rendered DOM |
| `browser_trutee.py` | Trutee | Next.js RSC app — scrape hydrated DOM |
| `browser_ezlinks.py` | EZLinks | Cloudflare JS challenge, then its API |
| `browser_golfnow.py` | GolfNow | capture the API predicate the page posts |
| `browser_golfwithaccess.py` | Troon / Golf With Access | SPA-only JSON |
| `browser_supersaas.py` | SuperSaaS | JS-rendered day view |
| `browser_tenfore.py` | TenFore | mint reCAPTCHA token in-browser |
| `browser_totale.py` | Total-e-Integrated | encrypted WebForms postback state |

## `probe_*.py` — diagnostics (no DB writes)

Read-only experiments used to work out *how* to reach a hard platform (plain vs
proxy vs Patchright vs curl_cffi). They print findings; they never write to D1.
Examples: `probe_cps_patchright.py`, `probe_teesnap.py`, `probe_proxy_diag.py`.
These pair with the `probe-*` GitHub workflows.

## Conventions worth keeping

- **Never swallow errors** — raise; the aggregator records per-course failures.
- **Defensive parsing** — platforms rename fields without notice.
- **Trustworthy empties only** — return `[]` for a genuinely empty sheet; return an
  error (not `[]`) if you're unsure, so `d1.py` shields existing rows.
- **Every `TeeTime` carries a `booking_url`** — we deep-link, never resell.
