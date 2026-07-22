# Colorado Tee-Time Aggregator

**Full-coverage build (July 2026).** All 241 Colorado golf courses researched;
146 have online booking across 14 platforms.

Key files:
- `colorado_golf_courses_booking.csv` — the master list: every course, its
  booking platform, booking URL, and confidence/notes.
- `registry.json` — machine registry generated from the CSV
  (`python build_registry.py` regenerates it after CSV edits).
- `scraper/` — platform adapters + aggregator CLI.
- `scraper/onetee.py` — posts aggregated data to OneTee (oneteeapp.com) once
  your API exists; until then `--export-csv` produces an importable file.
- `ARCHITECTURE.md` — design, platform landscape, legal notes, scaling plan.
- `docs/SETUP.md` — **deploy guide**: GitHub Actions + Cloudflare D1, $0/month.
- `.github/workflows/scrape.yml` — hourly scrape → D1 sync workflow.
- `worker/` — optional Cloudflare Worker read-API for oneteeapp.com.

Quick start (Python 3.10+, `pip install requests`):

    python -m scraper.aggregate --date 2026-07-25          # fetch everything
    python -m scraper.aggregate --platforms foreup,teeitup  # subset
    python -m scraper.onetee --export-csv output/tee_times.csv
    ONETEE_API_URL=https://your-endpoint ONETEE_API_KEY=... python -m scraper.onetee

Adapter status: foreup, teeitup, chronogolf, clubprophet implemented and
research-verified; quick18, teesnap, clubcaddie, membersports implemented
best-effort (may need one devtools capture to finalize per platform);
golfnow/ezlinks require partner access (bot-protected); 5 niche courses
(ForeTees, IBS Vision, SuperSaaS, Square) unsupported for now.

Note: this was built in a cloud sandbox that cannot reach booking APIs, so
bundled output is simulated (flagged). Run locally for live data.
