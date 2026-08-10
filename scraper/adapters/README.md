# `scraper/adapters/` — one module per booking platform

Each adapter knows how to ask one booking platform for tee times and translate the
reply into our normalized `TeeTime`. This is where "integrate with platforms, not
courses" lives: add one adapter, cover every course on that platform.

## The shared base (`base.py`)

- **`Adapter`** — abstract base. A subclass sets `platform = "<name>"` and
  implements `fetch(course, date) -> list[TeeTime]` for one course + one date.
- **`make_session()` / `USER_AGENT`** — a shared `requests.Session` with a
  desktop-Chrome UA and JSON `Accept` headers.
- **`get_json` / `post_json`** — polite HTTP with retry+jitter backoff on
  `429/500/502/503/504`; other 4xx re-raise immediately.
- **`base_tee_time(course, **kw)`** — the single factory that stamps every slot
  with course identity/city/state/platform/`booking_url`. Use it; don't build
  `TeeTime`s by hand.
- **`PartialFetchError`** — for multi-course venues: carries the slots that *did*
  answer plus `failed_labels`, so `d1.sync()` publishes the good sheets while
  shielding the failed ones' existing rows from deactivation.

Adapters **must raise on hard failure** (the aggregator catches and records it) and
must not return `[]` when unsure — an empty list means "confirmed no availability."

## How a course reaches its adapter

`aggregate.py` holds `ADAPTERS` (platform string → class) and
`get_adapter(platform)`; `other:*` platforms fall to `OtherAdapter`.

## Platform table

Transport legend: **JSON** = hidden JSON API via plain HTTP; **HTML** = parse
rendered HTML; **curl_cffi** = needs TLS impersonation; **browser** = handled by a
`scraper/browser_*.py` instead (stub here).

| platform key | Real platform | Transport | Key endpoint / host | Quirk to know |
|---|---|---|---|---|
| `foreup` | ForeUp | JSON | `foreupsoftware.com/index.php/api/booking/times` | needs `schedule_id` (+ usually `booking_class`) |
| `teeitup` | GolfNow TeeItUp (kenna) | JSON | `phx-api-be-east-1b.kenna.io/v2/tee-times` (`x-be-alias` header) | pin facility `id`, but slots carry only Mongo `courseId` — map via facilities |
| `chronogolf` | Chronogolf (Lightspeed) | JSON | `chronogolf.com/marketplace/clubs/<id>/teetimes` | must send `affiliation_type_id` or 422 |
| `clubprophet` | Club Prophet / CPS Golf | JSON (anon token) | `<tenant>.cps.golf/onlineres/onlineapi/…/TeeTimes` | anon bearer from public client_id + static headers; challenged tenants use `browser_cps.py` |
| `membersports` | MemberSports | JSON (POST) | `api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes` | `golfCourseId=0` = all courses; sweep `configurationTypeId` (in `experimental.py`) |
| `clubcaddie` | Club Caddie | browser (SPA) | `apimanager-<shard>.clubcaddie.com/webapi/view/<token>/slots` | client-rendered; parse `div.teetime` DOM (`browser_clubcaddie.py`) |
| `ezlinks` | EZLinks | JSON (via browser to clear CF) | `<portal>.ezlinksgolf.com/api/search/search` | one portal → many courses; fetch once per (portal,date), filter by name |
| `teesnap` | Teesnap | JSON (anon) | `<sub>.teesnap.net/customer-api/teetimes-day` | `?course=` resolves GLOBALLY — a wrong id serves another club's sheet; single-thread (`--workers 1`) |
| `quick18` | Quick18 (SagaCity) | HTML | `<sub>.quick18.com/teetimes/searchmatrix` | any `<tr>` starting with a time is a slot |
| `foretees` | ForeTees | JSON | `web.foretees.com/v5/servlet/Public_teesheet` | beyond `viewableDaysInAdvance` returns empty (correct) |
| `golfwithaccess` | Golf With Access (Troon) | browser | `golfwithaccess.com/api/v1/tee-times` | one live courseId; facility-named id is a dead aggregate |
| `rguest` / `agilysys` | rGuest Golf (Agilysys) | JSON (anon app token) | `book.rguest.com/wbe-golf-service/…/getAvailableTeeSlots` | 501 = empty; filter `isPrivate`; two platform keys, one class |
| `golfpay` | GolfPay | JSON (anon) | `golfpay.co/api/tee-times` | 422 = out of window → empty; filter `is_online_block` |
| `easytee` | EasyTee | HTML (anon) | `app.easyteegolf.com/course/<slug>/?days=<N>` | `days` is a relative offset; publish only if page's date matches |
| `golfrev` | GolfRev (Cybergolf) | curl_cffi | `golfrev.com/go/tee_times/teetime_table_html.asp` | plain 403; curl_cffi Chrome TLS → 200 (no proxy/browser) |
| `courseco` | CourseCo | JSON | `<gw>-gateway.totaleintegrated.net/Booking/Teetimes` | per-tenant gateway; `Origin` load-bearing; `-0.01` = price sentinel |
| `teequest` | TeeQuest (2 skins) | HTML | `bookateetime.teequest.com` / `teetimes.teequest.com` | v2 retries `selectedPlayers` 1→2 |
| `clubessential` | ClubEssential / NetCaddy | JSON (no auth) | `<host>/…/netcaddy/api/teetimes/Available` | courseId pinned, never guessed; assert SiteID per row |
| `resortsuite` | ResortSuite (Agilysys) | SOAP/XML | `<host>/wso2wsas/services/RSWS?action=FetchGolfTeeSheet` | drive `CourseId` in the service; never read privacy name fields |
| `golfback` | GolfBack | JSON (POST) | `api.golfback.com/api/v1/courses/<uuid>/date/<d>/teetimes` | must POST (GET→405); read `localDateTime`, ignore mislabelled UTC |
| `tenfore` | TenFore | JSON (open endpoint) | `swan.tenfore.golf/api/TeeSheet` | priced endpoint is reCAPTCHA-gated; use the open TeeSheet |
| `totale` | Total-e-Integrated | browser | — | date in encrypted `__VIEWSTATE`; `browser_totale.py` owns it (stub here) |
| `golfnow` | GolfNow / EZLinks | (stub) | — | bot-protected; production path is the affiliate feed |
| `other:*` | niche/unsupported | (stub) | — | `OtherAdapter` raises with the booking URL for visibility |

`experimental.py` holds `MemberSportsAdapter`, `GolfNowAdapter`, `TotaleAdapter`,
`OtherAdapter`.

## Adding or fixing an adapter

1. Find the platform's real data call (open the course's booking page, watch the
   Network tab for the JSON/HTML request that returns tee times).
2. Copy the closest existing adapter; implement `fetch`; build slots with
   `base_tee_time`.
3. Register it: import + add to `ADAPTERS` in `aggregate.py`.
4. Add the platform's ID extraction to `build_registry.py` if the registry needs
   new ID fields.
5. Test one course: `python -m scraper.aggregate --date <d> --platforms <p> --courses <slug> -v`.
6. If plain HTTP is blocked, check the block-type taxonomy in
   `docs/ARCHITECTURE.md` §3 before reaching for a full browser — curl_cffi or
   Patchright is often enough.
