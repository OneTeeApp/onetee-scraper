# Tee-Time Aggregator — Architecture & Technical Plan

*Colorado pilot → US-wide. Written July 2026.*

## 1. The core insight

There is no single source of tee times. The market is fragmented across roughly a dozen booking platforms, and each course licenses one of them. That fragmentation is the entire reason an aggregator is valuable — and it dictates the architecture: **you don't integrate with courses, you integrate with platforms.** Colorado's 181 publicly bookable courses collapse into ~9 platform integrations, and those same 9 integrations already cover a large share of the ~11,000 public courses in the US.

What we learned mapping Colorado (July 2026, 36 platform-confirmed courses):

| Platform | CO courses found | Public JSON API? | Difficulty |
|---|---|---|---|
| ForeUp | ~10 | Yes — same API the booking SPA calls | Easy |
| GolfNow TeeItUp | ~8 | Yes (`phx-api-be-east-1b.kenna.io`, alias header) | Easy |
| Club Prophet (cps.golf) | ~6 | Yes, per-tenant; some need SPA headers | Medium |
| Chronogolf / Lightspeed | ~3 | Yes (marketplace API) + official Partner API | Easy–Medium |
| MemberSports | ~3 portals | SPA JSON, needs one-time devtools capture | Medium |
| Club Caddie | ~2 | SPA JSON behind sharded hosts | Medium |
| GolfNow / EZLinks | ~3+ | No — bot-protected; partner feed is the path | Hard |
| Teesnap | 1 | Per-deployment customer API | Medium |
| None/unknown yet | ~145 | — | discovery work |

Two structural facts worth internalizing early. First, **platform churn is real**: in the months since the source CSV was compiled, at least 4 of 36 confirmed courses switched platforms (Cattail Creek ForeUp→Club Prophet, Cattails ForeUp→Teesnap, Collindale ForeUp→Chronogolf, Meadows ForeUp→MemberSports). The system must detect churn, not assume a static registry. Second, **municipal portfolios are leverage**: one portal often covers many courses (Denver Golf's single MemberSports portal covers 7 courses; City of Aurora's EZLinks portal covers 5; City of Loveland's Club Prophet portal likely covers 3). Mapping portals, not courses, is the efficient unit of discovery work.

## 2. System components

```
                ┌────────────────────────────────────────────┐
                │              Course Registry               │
                │  (courses, platforms, platform IDs, status)│
                └──────┬─────────────────────────┬───────────┘
                       │                         │
        ┌──────────────▼──────────┐   ┌──────────▼──────────────┐
        │   ID-Discovery Pipeline │   │     Fetch Scheduler      │
        │  (find booking URLs +   │   │  (which course, which    │
        │   platform IDs; detect  │   │   date, how often)       │
        │   churn)                │   └──────────┬──────────────┘
        └─────────────────────────┘              │
                                      ┌──────────▼──────────────┐
                                      │   Platform Adapters      │
                                      │ foreup / teeitup / …     │
                                      └──────────┬──────────────┘
                                      ┌──────────▼──────────────┐
                                      │  Normalizer → TeeTime    │
                                      └──────────┬──────────────┘
                                      ┌──────────▼──────────────┐
                                      │  Store (Postgres) + API  │
                                      └──────────┬──────────────┘
                                      ┌──────────▼──────────────┐
                                      │  Search front-end        │
                                      └─────────────────────────┘
```

The prototype in this repo implements the adapters, normalizer, registry, and a file-based store; scheduler and Postgres come next.

### Course registry
`registry.json` today; a Postgres table soon. One row per *bookable unit* (usually a course, sometimes a multi-course portal). Fields: slug, name, geo (city/lat/lon), platform, platform-specific IDs, `status` (ready / needs_ids / experimental), provenance notes, `last_verified_at`. The registry is the crown jewel — the scraped tee times are perishable, but the registry is accumulated knowledge.

### ID-discovery pipeline
Each platform needs slightly different identifiers (ForeUp course+schedule+booking-class IDs, TeeItUp alias, Chronogolf club UUID, CPS tenant, …). Discovery is a semi-automated pipeline:

1. From the course website, find the "book a tee time" link (fetch + regex/LLM extraction). This alone classifies the platform ~90% of the time from the URL shape.
2. From the booking page, harvest IDs. Static HTML suffices for some platforms; SPA platforms (TeeItUp, MemberSports, Club Caddie) need either a headless browser (Playwright) capturing the page's own API calls, or a one-time manual devtools capture.
3. Verify with one live API call; record `last_verified_at`.

Re-run monthly against all courses, plus on-demand when a course's fetch error rate spikes — that's your churn detector. The prototype ships `discover_ids()` helpers on the ForeUp and Chronogolf adapters.

### Platform adapters
One module per platform behind a common interface (`fetch(course, date) -> list[TeeTime]`). Rules that keep this maintainable at scale: adapters never swallow errors (the aggregator records them per course); parsing is defensive because platforms version-drift field names; every adapter records the raw payload for replay/debugging; rate limits and politeness live in shared middleware, not per adapter.

### Normalized model
`TeeTime`: course, platform, ISO local datetime, holes options, open spots, price min/max, currency, booking deep-link, raw payload. Two normalization gotchas found already: TeeItUp returns UTC timestamps and prices in cents; Chronogolf models pricing per-player via "affiliation types" rather than per-slot. Normalize time zones at ingest (all Colorado is America/Denver; nationally, store course tz in the registry).

### Store & serving API
Prototype writes one JSON document per (date) run. Production: Postgres with tables `courses`, `tee_time_snapshots` (append-only, for freshness/history) and a `tee_times_current` materialized view the search API reads. Tee times are perishable (minutes-level staleness matters for popular Saturday slots), so serve with a `fetched_at` timestamp and let the UI show freshness honestly. Add PostGIS early — "tee times near me next Saturday morning under $80" is the killer query and it's geo+time+price.

### Fetch scheduler
Not cron-per-course; a priority queue. Refresh frequency should follow demand and volatility: next-72-hours slots at popular front-range courses every 10–15 min; shoulder dates hourly; 10+ days out daily. This cuts request volume by ~10x versus uniform polling, which matters both for cost and for being a polite client.

## 3. Legal & relationship strategy (read this before scaling)

This is factual context, not legal advice — talk to a lawyer before commercial launch.

- **Facts you're aggregating** (course, time, price, availability) are generally not copyrightable, and US case law (e.g. *hiQ v. LinkedIn*) has been relatively favorable to scraping publicly accessible data — but ToS, CFAA edge cases, and bot-protection circumvention are all live issues. GolfNow/EZLinks actively bot-protect; treat that as a "no" and use their partner program instead of fighting it.
- **robots.txt**: ForeUp disallows crawling under `/index.php/`. A personal-use prototype hitting the same endpoint a human's browser hits, at human rates, is one thing; a commercial crawler is another. Respect crawl policies in production and get explicit access where the policy says no.
- **The sanctioned paths exist and are better anyway**: Lightspeed Golf has an official Partner API (partner-api.docs.chronogolf.com); GolfNow has an affiliate/distribution program; ForeUp and Club Prophet do course-authorized integrations. The durable business is *distribution partner*, not *scraper* — booking platforms want fills, courses want tee sheets full, and an aggregator that sends them bookings (with attribution/affiliate links) is aligned with everyone. Scraping is your bootstrap and gap-filler, not the end state.
- Don't cache-and-resell stale prices as current; always deep-link to the platform's booking page for the actual transaction (the prototype does this — every TeeTime carries a `booking_url`).

## 4. Scaling from Colorado to the US

Phase 1 (now): Colorado, platform-confirmed courses (~36), adapters for ForeUp/TeeItUp/Chronogolf/CPS. Prove the normalized model and the demo UX.

Phase 2: finish Colorado coverage — run the ID-discovery pipeline across the ~145 unmapped courses (most will land on the same platforms; expect a long tail of Facebook-page-only 9-holers with no online booking at all — mark them `no_online_booking` and move on). Stand up Postgres + scheduler.

Phase 3: nationalize course inventory. Seed from public datasets (USGA course database, NGF, OpenStreetMap `leisure=golf_course`) → ~14k US facilities; run the same discovery pipeline. Because it's platform-keyed, each new state is mostly registry work, not new code. Prioritize states by golfer demand (FL, CA, TX, AZ, NC…).

Phase 4: partnerships and monetization — GolfNow affiliate, Lightspeed partner API, direct course deals; booking attribution; alerts ("tell me when a Saturday morning slot opens at Fossil Trace").

International later: same architecture, new platform set (BRS Golf/GolfNow UK, intelligentgolf, Chronogolf is already strong in Canada).

## 5. Prototype contents & how to run

```
teetime/
├── courses.csv               # source inventory (241 CO courses)
├── registry.json             # 36 courses with platform IDs (+ corrections)
├── scraper/
│   ├── models.py             # TeeTime / FetchResult
│   ├── aggregate.py          # CLI: fetch all → output/tee_times.json
│   ├── gen_sample.py         # simulated data in the same schema (demo/dev)
│   └── adapters/
│       ├── base.py           # Adapter ABC + shared HTTP session
│       ├── foreup.py         # + discover_ids()
│       ├── teeitup.py        # + discover_facilities()
│       ├── chronogolf.py     # + discover_ids()
│       ├── clubprophet.py
│       └── experimental.py   # membersports, clubcaddie, golfnow, teesnap
├── output/tee_times.json     # aggregated output (currently simulated)
└── demo/index.html           # searchable demo front-end (data embedded)
```

Run it (Python 3.10+, `pip install requests`):

```bash
# live fetch, two courses, day after tomorrow
python -m scraper.aggregate --date 2026-07-23 --courses breckenridge,vail-golf-club

# everything that's ready
python -m scraper.aggregate --platforms foreup,teeitup,clubprophet

# fill missing ForeUp schedule IDs
python -c "from scraper.adapters.foreup import ForeUpAdapter; print(ForeUpAdapter().discover_ids('22979'))"

# regenerate demo data offline
python -m scraper.gen_sample --date 2026-07-25
```

Note: the cloud sandbox this was built in cannot reach booking APIs (egress allowlist), so `output/tee_times.json` currently holds **simulated** data flagged `"simulated": true`. Run the aggregator from your own machine to replace it with live data; the demo page can be rebuilt with `python build_demo.py`.

## 6. Immediate next steps

1. Run `python -m scraper.aggregate` locally against the 19 `ready` courses; fix any field-drift the defensive parsers surface (send me the `errors` array and raw payloads and I'll tighten the adapters).
2. Capture the MemberSports and Club Caddie JSON calls once via browser devtools (or Claude-in-Chrome) to graduate those adapters from experimental.
3. Harvest missing IDs: ForeUp schedule/booking-class IDs (4 courses), TeeItUp facility IDs (auto via `discover_facilities`), Chronogolf UUIDs (3 clubs).
4. Point the ID-discovery pipeline at the 145 unmapped Colorado courses.
5. Postgres + scheduler; then the front-end goes from demo to product.
