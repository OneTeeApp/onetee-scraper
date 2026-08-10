# Glossary — plain-English definitions

Terms used throughout OneTee, for anyone (coder or not) reading the code or docs.

### The business

- **Tee time** — a specific time you can start a round of golf at a course
  (e.g. "7:40 AM at Applewood, 18 holes, $58, up to 4 players").
- **Aggregator** — a service that gathers listings from many places into one
  searchable spot. OneTee aggregates tee times the way a flight site aggregates
  airlines.
- **Booking platform / booking engine** — the software a golf course rents to sell
  tee times online (ForeUp, TeeItUp, Club Prophet/CPS, Teesnap, Chronogolf,
  ClubCaddie, EZLinks, GolfNow, …). Each course uses exactly one. OneTee talks to
  the *platform*, so one integration covers every course on it.
- **Course vs. venue** — a "venue" is a physical golf property; it can contain
  more than one "course" (e.g. a North and South course). Some booking sources are
  duplicates of the same venue, so we group them with a `venue_id`.

### The pipeline

- **Registry** (`registry.json`) — our machine-readable master list of courses we
  can scrape, each with its platform and the ID numbers that platform needs.
- **Directory** (`directory.json`) — the broader list of *all* courses, including
  ones with no online booking (so the site can say "call to book").
- **Adapter** — a small piece of code that knows how to talk to one booking
  platform and translate its reply into our standard format.
- **Browser fetcher** — like an adapter, but drives a real web browser because the
  platform blocks plain requests or only shows data after JavaScript runs.
- **TeeTime** — our standard, normalized shape for one open slot, so every
  platform's data looks the same once it's in our system.
- **Aggregator** (`scraper/aggregate.py`) — the program that runs all the adapters
  for a given date and collects the results. (Same word as the business concept;
  here it's the specific script.)
- **Normalize** — convert different platforms' formats into one consistent shape
  (e.g. fix time zones, convert prices from cents to dollars).

### Storage & serving

- **D1** — Cloudflare's cloud database (SQLite-compatible). It stores every tee
  time we've found, one row per open slot.
- **Worker** — a small program that runs on Cloudflare's servers worldwide and
  answers the website's data requests by reading D1. It's the "read API."
- **API** (Application Programming Interface) — a URL the website calls to get data,
  e.g. `/api/tee-times?state=CO&date=2026-08-11`. Returns JSON.
- **JSON** — a plain-text data format that both code and humans can read; how the
  API and scrapers pass data around.
- **Freshness guard / `sheet_freshness`** — a safety net in the Worker. Each time a
  course is successfully scraped, we stamp "confirmed at <time>". If a scraper
  breaks, the Worker notices the stamp is old and **hides** that course's times
  from the site, so golfers never see availability that might be wrong.
- **`last_seen_at` vs freshness** — a common gotcha: `last_seen_at` on a tee-time
  row only changes when the *price or availability* changes, so it can look "old"
  even when scraping is healthy. Judge scraper health by the freshness ledger and
  the run logs, **not** `last_seen_at`.
- **Elapsed / pruning** — a tee time whose start time has passed. Booking sites keep
  showing the whole day's grid, so we deactivate past slots ourselves (a Cloudflare
  cron every 5 minutes) to avoid showing this-morning's slots in the afternoon.

### Scheduling (how scrapers run)

- **GitHub Actions** — GitHub's free service for running scripts on a schedule or
  on demand. All our scrapers run here.
- **Workflow** — one scheduled job definition (a `.yml` file in
  `.github/workflows/`).
- **Near / mid / far tiers** — we refresh dates close to today very often (they
  change the most as people book/cancel) and far-off dates rarely. Near = days 0–3,
  mid = days 3–7, far = days 8–30.
- **Self-chaining loop** — instead of relying on a timer, a workflow re-launches
  itself when it finishes, so the near tier runs almost continuously. A timer
  ("cron") is kept only as a backup in case a run crashes.
- **Cron** — a schedule expression like `17 * * * *` (meaning "at minute 17 of
  every hour").
- **Sharding** — splitting the full course list into N equal slices so N copies of a
  job can run in parallel and finish faster. `--shard 1/4` = "handle slice 1 of 4."
- **Watchdog** — a workflow that runs every 15 minutes to restart any self-chaining
  loop that has died.
- **Concurrency group** — a GitHub setting that stops two copies of the same job
  from running at once; the second one waits in line.

### Anti-bot / access

- **Proxy** — a relay server that makes our request appear to come from a different
  IP address. Used when a platform blocks or rate-limits our server's address. A
  **rotating datacenter proxy** gives a new address on every request.
- **Cloudflare challenge** — a bot check some platforms put in front of their site.
  A plain script fails it; a real browser passes it.
- **Patchright** — a stealth version of the Playwright browser tool that hides the
  tell-tale signs of automation, letting us clear Cloudflare's bot check from a
  cheap datacenter server without a proxy.
- **curl_cffi** — an HTTP library that mimics a real Chrome's TLS "fingerprint,"
  which is enough to get past some (fingerprint-based) blocks without a full
  browser.
- **reCAPTCHA** — Google's "prove you're human" check; a few platforms require a
  token that can only be minted inside a real browser.

### Other

- **Slug** — a short, URL-safe id for a course, e.g. `applewood-golf-course`.
- **Cloudflare** — the company hosting our database (D1), API (Workers), and edge
  network.
- **Squarespace** — the website builder hosting oneteeapp.com; the search widget
  there calls our Worker API.
