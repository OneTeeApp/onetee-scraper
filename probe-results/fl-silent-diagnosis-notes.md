# Florida silent-course diagnosis: one method that works, one that does not

2026-07-29. Written after a diagnosis that reached four confident, plausible,
and unsupported verdicts. Recording it so the next session does not spend the
same requests to reach the same wrong answer.

## What was being asked

Florida's fill plateaued at 16:12Z — teeitup 155 serving / 58 silent, up just
one venue in the five and a half hours after 10:49Z. Sweep latency had
explained the earlier silence (13 of the 16 rows commit e7964e1 added went
live within five hours) but it cannot explain a plateau. So: which of the
remaining silent rows are broken, and which are simply empty?

## The method that FAILED: reading platform club pages with WebFetch

Five Florida chronogolf rows are registry-`ready` and serving nothing:
east-bay-golf-club, manatee-county-golf-course, ocean-palm-golf-course,
ocean-village-golf-course, river-hall-country-club.

Fetching `chronogolf.com/club/<slug>` for four of them returned, each time, a
confident and identical-sounding verdict: *unclaimed directory listing, no
booking UI present, no-online-booking pin on the map.* That matches a real and
already-documented class — the AZ CSVs carry seven `other:unclaimed-chronogolf`
rows described exactly that way — so the finding felt like a confirmation.

Then the control. **gator-trace-golf-country-club was fetched the same way and
produced the same "unclaimed directory listing, no booking UI" verdict — and
Gator Trace is serving tee times in production right now.** It came live in the
08:40Z measurement and is in the current inventory sample.

So the method cannot distinguish a claimed club from an unclaimed one. The
booking UI on these pages is client-rendered; WebFetch does not execute it, so
every club page looks unclaimed. The four verdicts were discarded and **no
retag was made**. Had the control been skipped, four courses would have been
moved to `unsupported` on the strength of a measurement that says nothing.

Corollary worth stating plainly: a live `chronogolf.com/club/<slug>` page is
not evidence of a tee sheet, and a *dead-looking* one is not evidence of its
absence. Both directions of that inference are unavailable from plain HTML.

The same trap applies to teesnap. The control portal
`dogwoodlakesgc.teesnap.net` returns 200 with the banner *"Tee times are
currently unavailable due to maintenance"* — while Dogwood Lakes is serving in
production. The rendered page is not the customer API the adapter reads.

## The one signal that did survive

`deercreek.teesnap.net` and `placidlakescc.teesnap.net` both return **HTTP
500**, against a control portal on the same platform returning 200. That is a
transport-level asymmetry rather than a content reading, so it is worth
following up — but it is still the HTML host, not the API the adapter calls,
and a single 500 can be transient. It justifies a probe, not a retag.

## What actually answers this

`scripts/probe_teeitup_fl.py`, which hits kenna's real facilities/sheet/fetch
paths with serving controls and self-invalidates when the controls do not
serve. It is still not runnable: the workflow sits at
`probe-results/probe-teeitup-fl.yml.pending` because the PAT that wrote it is
contents-scoped and GitHub refuses workflow files without `workflow` scope.
Moving that one file into `.github/workflows/` arms it.

A chronogolf equivalent would need the same shape — the marketplace API with a
known-serving control — not the club page.

## Standing rule this reinforces

Establish the positive control BEFORE reading any verdicts, not after. The
repo already learned this for `dig`/`nslookup` (missing tools print nothing,
which reads as "no record") and for kenna 429 storms (which turn every target
empty, which reads as a dead fleet). This is the third instance, and the first
where the failing method returned prose confident enough to act on.
