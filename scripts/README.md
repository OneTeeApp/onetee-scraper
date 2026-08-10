# `scripts/` — one-off tools & diagnostics

~49 utilities accumulated during development. **Most are throwaway** — frozen
snapshots of a specific bug hunt, kept for reference. A handful are reusable tools.
None are part of the recurring pipeline (that's `scraper/` + `.github/workflows/`).

Their saved outputs live in `../probe-results/` (text/JSON dumps — investigation
artifacts, not live data).

## Reusable tools (safe to depend on)

| Script | What it does |
|---|---|
| `monitor_inventory.py` | Per-state "is the site actually serving fresh tee times?" witness (run hourly by `monitor-inventory.yml`). |
| `probe_staleness.py` | Find (and with `--deactivate`, clean up) stale/orphaned live inventory. |
| `state_status.py` / `state_dashboard.py` | Per-state coverage reporting. |
| `enrich_phones.py` | Scrape public phone numbers to enrich the directory. |
| `co_regression.py` / `state_regression.py` | Coverage regression checks (CI-runnable). |
| `verify_directory.py` | Check the directory against source truth. |
| `test_*.py` | Offline adapter fixture tests (`test_teeitup_adapter.py`, `test_teesnap_adapter.py`, `test_membersports_adapter.py`, `test_d1_orphans.py`). |

## Throwaway diagnostics (read, don't rely on)

Grouped by prefix — each is a frozen investigation whose *findings* live in the
project changelog / docs, not in the script:

- `diag_*` — per-platform/course bug hunts (teesnap, golfnow, kenna, subcourses).
- `probe_*` — reachability/lead experiments (per-state teeitup, needs-ids, leads).
- `verify_*` — one-time fix verifications.
- `co_*` — Colorado-specific coverage/regression checks.
- `crosscheck_*`, `flap_*` — GolfNow flap/404 investigations.
- data-fix one-offs — `apply_fl_unknown_resolution.py`, `md_apply_rev2.py`,
  `convert_florida_xlsx.py`, `derive_windows.py`.

If you're hunting a similar bug, the closest `diag_*`/`probe_*` is a good starting
template — but expect to update hard-coded ids/dates.
