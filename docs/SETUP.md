# Deploy: GitHub Actions → Cloudflare D1

Total cost: **$0/month** (GitHub Actions free on public repos; D1 free tier —
the diff-based sync keeps writes ~100× under the 100k/day limit).

## 1. Create the Cloudflare D1 database (~3 min)

1. Sign up / log in at https://dash.cloudflare.com (free plan is fine).
2. In the left sidebar: **Storage & Databases → D1 SQL Database → Create**.
   Name it `onetee-teetimes`. Copy the **Database ID** shown on its page.
3. Note your **Account ID** (right side of any dashboard page, or in the URL).
4. Create an API token: **My Profile → API Tokens → Create Token → Custom**.
   Give it one permission: `Account → D1 → Edit`. Copy the token.

(Prefer CLI? `npx wrangler d1 create onetee-teetimes` prints the same ID.)

## 2. Create the GitHub repo (~3 min)

1. Create a new **public** repo on GitHub (public = unlimited free Actions
   minutes; the code and course list have nothing secret in them).
2. Push this folder to it:

       git remote add origin https://github.com/<you>/onetee-scraper.git
       git push -u origin main

   (This folder is already a git repo with an initial commit.)

## 3. Add the secrets (~2 min)

Repo → **Settings → Secrets and variables → Actions → New repository secret**,
three times:

| Secret name             | Value                        |
|-------------------------|------------------------------|
| `CLOUDFLARE_ACCOUNT_ID` | your account ID              |
| `CLOUDFLARE_API_TOKEN`  | the D1-edit token            |
| `CLOUDFLARE_D1_DB_ID`   | the database ID from step 1  |

## 4. Run it

Repo → **Actions → Scrape tee times → Run workflow**. The workflow also runs
automatically every hour (and on every push). Each run scrapes today + the
next 2 days, diff-syncs into D1, and uploads the raw JSON as a 7-day artifact.

Check the data anytime:

    python -m scraper.d1 stats            # with the three env vars set locally

or in the Cloudflare dashboard → your D1 database → Console:

    SELECT COUNT(*), SUM(active) FROM tee_times;
    SELECT * FROM runs ORDER BY id DESC LIMIT 5;

## 5. (Optional) Read API for oneteeapp.com

`worker/` contains a Cloudflare Worker that exposes the database as JSON:

    cd worker
    # put your database ID into wrangler.toml first
    npx wrangler deploy

Endpoints: `/api/health`, `/api/courses`,
`/api/tee-times?date=2026-07-25&city=Denver&max_price=80&min_spots=2`.
Point the OneTee front-end at these (CORS is open; tighten
`Access-Control-Allow-Origin` to your domain when live). Workers free tier =
100k requests/day.

## Notes & maintenance

* **Scheduled-workflow gotchas:** GitHub disables cron workflows after 60 days
  with no repo activity — any commit resets the clock. Runs can start a few
  minutes late; that's normal.
* **Cadence:** hourly now; if you tighten to every 30 min, watch
  `runs.rows_*` columns — writes stay tiny thanks to diff sync.
* **Which courses actually return data** will depend on how each platform
  treats GitHub's (Azure) IP ranges — the `runs.errors` column tells you
  exactly who's blocking. If a platform consistently 403s, that's the signal
  to run that adapter from a residential IP (Pi at home) or use a proxy.
* **Simulated data:** if you see `simulated=1` rows, they came from
  `gen_sample.py` during development — real Actions runs write live data only.
  Clear dev rows with `DELETE FROM tee_times WHERE simulated=1;`
* `python -m scraper.d1 push --local test.db` runs the whole pipeline against
  a local SQLite file — handy for development without touching production.
