# Pending workflows — two files, one commit, both blocked things unblock

These are finished GitHub Actions workflows that are not in
`.github/workflows/` yet, for one boring reason: the sessions that wrote them
hold a **contents-scoped** fine-grained PAT, and GitHub refuses any push that
creates or edits a workflow file without `workflow` scope:

```
refusing to allow a Personal Access Token to create or update workflow
.github/workflows/... without `workflow` scope
```

Nothing else about them is provisional. Both were written against the shape of
the workflows already in this repo, and both are read-only: public GETs, no
Cloudflare secrets, no D1 writes, no registry or CSV edits.

## Arming them

```bash
git mv probe-results/pending-workflows/probe-teeitup-fl.yml       .github/workflows/
git mv probe-results/pending-workflows/browser-golfback-probe.yml .github/workflows/
git commit -m "Arm the pending FL probes"
git push
```

Or paste each file in the GitHub web editor — a browser session has the scope a
PAT lacks. Either way, delete this README once the folder is empty.

Both carry a `push:` path on their own script, so once armed, **landing a change
to the script is the request to run it** — no further manual dispatch, and no
future session needs to ask.

## What each one answers

**`probe-teeitup-fl.yml`** → `scripts/probe_teeitup_fl.py`

Which silent TeeItUp courses are *broken* versus merely *empty*. Florida has 57
registry-`ready` teeitup rows serving nothing, and the fill has plateaued (253
venues across three samples), so latency no longer explains them. The probe asks
kenna's facilities route, the sheet bare and pinned across three dates, and the
real production fetch path, with two controls that served in all four of the
day's inventory samples. It self-invalidates if the controls come back empty,
because a kenna 429 storm makes every course look dead — two Arizona probe runs
were discarded for exactly that.

Note: much of the silent cohort is *also* now covered from another direction.
`scripts/probe_newly_ready.py` was extended to probe silent rows through the
real fetch path on every platform, and `probe-newly-ready.yml` was already
armed, so that one needed no new file. This probe is still the only thing that
can distinguish a wrong alias from an empty sheet, which the fetch path alone
cannot.

**`browser-golfback-probe.yml`** → `scripts/browser_golfback_probe.py`

What API a GolfBack tee sheet actually calls. GolfBack is the largest
unsupported platform in the registry — 17 Florida courses, every uuid already
captured, no adapter — and the request shape is not observable from outside a
browser:

* `golfback.com` serves its SPA shell for **every** path, so its HTML says
  nothing about its API.
* `api.golfback.com` is a live JSON host (`GET /` returns
  `{"happy":"Somebody's closer!","version":"Version 1.4.5.119+edc3"}`) but
  publishes no Swagger and 404s every guessed route. Seven plausible REST shapes
  were tried and all missed; more guessing was the wrong instrument.

The probe loads two courses on different tenants, records every XHR/fetch with
method, URL, load-bearing headers, POST body and a bounded response sample, then
moves the date and records again — the difference between the two captures is
what names the date parameter. Two courses rather than one so a per-course
identifier can be told apart from a platform constant; mistaking one for the
other is how an adapter ends up publishing a single club's sheet under every
name. Auth-shaped header values are redacted before anything is written.

Its output, `probe-results/golfback-api.json`, is everything needed to write
`scraper/adapters/golfback.py`. Once that adapter lands, the 17 rows move from
`unsupported` to `ready` by adding `golfback` to `IMPLEMENTED` in
`build_registry.py` and retagging `other:golfback` to `golfback` in the Florida
CSV — the uuids are already extracted and waiting.
