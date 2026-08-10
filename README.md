# OneTee — Golf Tee-Time Aggregator

OneTee lets a golfer search **one** website for available tee times across
hundreds of golf courses, instead of visiting each course's booking page one by
one. It's live at **oneteeapp.com**.

New here? Read this page, then the **[docs/GLOSSARY.md](docs/GLOSSARY.md)** if any
word is unfamiliar. Want the file-by-file tour? **[docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md)**.
Want the engineering design? **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## The one idea that explains everything

There is no single database of golf tee times. Each course rents its online
booking from one of ~24 different "booking platforms" (ForeUp, TeeItUp, Club
Prophet, Teesnap, …). So the trick is: **you don't integrate with 15,000
courses, you integrate with ~24 platforms.** Learn to talk to each platform
once, and you cover every course that uses it.

That single idea shapes the whole system. Everything below is just plumbing to
(1) find out which platform each course uses, (2) ask that platform for its tee
times on a schedule, (3) store the answers, and (4) serve them to golfers.

---

## How the data flows (the whole system in one picture)

```
 Per-state spreadsheets            registry.json              GitHub Actions
 of courses + booking URLs   ──▶   (machine list of      ──▶  runs the scrapers
 (*_golf_courses_booking.csv)      scrapable courses          on a schedule (free)
        build_registry.py          + their platform IDs)              │
                                                                      ▼
                                                          Platform adapters &
                                                          browser fetchers ask
                                                          each booking platform
                                                          "what's open on this date?"
                                                                      │
                                                          normalized into one shape
                                                          (a "TeeTime")
                                                                      ▼
   Golfer on oneteeapp.com  ◀──  Cloudflare Worker   ◀──  Cloudflare D1 database
   (Squarespace widget)          read-only JSON API        (stores every tee time,
   frontend/*.js                 worker/index.js            one row per open slot)
                                                            scraper/d1.py writes it
```

Read that top-to-bottom for the write path (scraping), bottom-to-top for the
read path (a golfer's search).

---

## What each part is (plain English)

- **The registry** (`registry.json`) — the machine-readable list of every course
  we *can* scrape, each tagged with its platform and the IDs that platform needs.
  Built from human-edited spreadsheets by `build_registry.py`. This is the "crown
  jewel": tee times are perishable, but the accumulated knowledge of *who uses
  what* is what took months to gather.

- **Adapters** (`scraper/adapters/`) — one small module per booking platform. Each
  knows how to ask that one platform for tee times and translate the answer into
  our common shape. Most talk to a platform's hidden JSON API directly (fast,
  cheap). See `scraper/adapters/README.md`.

- **Browser fetchers** (`scraper/browser_*.py`) — for platforms that block simple
  requests (bot protection, or pages that only render in a real browser), we drive
  an actual Chrome browser to get the data. Slower, used only where necessary.

- **The aggregator** (`scraper/aggregate.py`) — the conductor. Given a date, it
  loops over the registry, calls the right adapter for each course, and writes all
  the results to one file. `python -m scraper.aggregate --date 2026-08-11`.

- **The database sync** (`scraper/d1.py`) — takes that file and updates the
  Cloudflare **D1** database: adds new open times, updates changed prices,
  marks vanished times as gone. Also stamps a "freshness" record so the website
  knows the data is current.

- **The Worker API** (`worker/index.js`) — a tiny always-on program at Cloudflare's
  edge that reads the database and answers the website's questions
  (`/api/tee-times?state=CO&date=…`). It also **hides stale data**: if a scraper
  breaks, that course's times quietly disappear from the site instead of showing
  wrong availability. See `worker/README.md`.

- **The scheduler** (`.github/workflows/`) — ~17 recurring GitHub Actions
  workflows run the scrapers around the clock for free. Nearby dates are refreshed
  every few minutes; far-off dates less often. See `.github/workflows/README.md`.

- **The frontend** (`frontend/*.js`) — small scripts embedded in the Squarespace
  site that render the results and the "book by phone" directory.

- **The directory** (`directory.json`, `build_directory.py`) — a separate list of
  *every* course (even ones with no online booking), so the site can show "call
  this course to book" instead of pretending the course doesn't exist.

---

## Tech stack

- **Python 3.10+** — all scrapers (`requests` for HTTP; Playwright/Patchright for
  browser fetchers).
- **Cloudflare D1** — the SQLite-compatible database that stores tee times.
- **Cloudflare Workers** — the read API in front of D1.
- **GitHub Actions** — runs every scraper on a schedule, for $0/month.
- **Squarespace** — hosts oneteeapp.com; the widget calls the Worker API.

Coverage today: Colorado, Arizona, Florida, Virginia, Maryland, Utah.

---

## Quick start (for an engineer)

```bash
pip install requests

# Scrape one date across all courses → output/tee_times.json
python -m scraper.aggregate --date 2026-08-11

# Just one or two platforms
python -m scraper.aggregate --date 2026-08-11 --platforms foreup,teeitup

# Push results into a LOCAL SQLite copy (no cloud creds needed) and inspect
python -m scraper.d1 push --data output/tee_times.json --local
python -m scraper.d1 stats --local

# Rebuild the registry after editing a *_golf_courses_booking.csv
python build_registry.py
```

Writing to the real cloud database needs the `CLOUDFLARE_*` secrets (see
`docs/SETUP.md`). The scrapers are designed to run on GitHub's servers, which
already have those secrets configured.

---

## Where to go next

| You want to… | Read |
|---|---|
| Understand a term | `docs/GLOSSARY.md` |
| Find where something lives | `docs/CODEBASE_MAP.md` |
| Understand the design & trade-offs | `docs/ARCHITECTURE.md` |
| Work on the scrapers | `scraper/README.md` |
| Add/fix a platform | `scraper/adapters/README.md` |
| Understand the API / freshness | `worker/README.md` |
| Understand the scheduled jobs | `.github/workflows/README.md` |
| Deploy it yourself | `docs/SETUP.md` |
| Understand scaling to more states | `docs/SCALING.md` |
```
