"""Is the GolfNow 404 about the course, or about how fast we are asking?

WHERE THIS CAME FROM
--------------------
Two runs, two different sets of 404s, and the crosscheck's controls flipped:
Cedaredge — a course we serve today — 404'd on its own page thirteen minutes
after the identical request returned 99 tee times. So the 404 is not a property
of the facility and not a property of the page.

The thing both runs have in common is that the failures CLUMP. In the
crosscheck, twelve requests 1.2s apart came back 200,200 then 404,404,404 then
200,200,200,200 then 404,404,404 — runs of three, not a coin flip. In the
diagnosis, Black Bear failed six times in a row while Cedaredge succeeded three
in a row. That is the signature of a rate limiter, not of a missing record, and
some edges do answer a throttled request with a 404 rather than a 429.

If that is what this is, the production adapter is doing exactly the wrong
thing: three attempts back to back with 2.5s/5s/7.5s waits keeps it inside the
penalty window, so all three fail and the course is recorded as broken.

THE TEST
--------
One page. Alternate two predicates — a course that works and a course that
never does — and vary only the spacing.

  fast phase   20 requests, 3s apart, alternating
  slow phase    8 requests, 20s apart, alternating

  failures clump regardless of which course  -> RATE. Slow down and back off
                                                properly; nothing is wrong with
                                                the four facilities.
  one course always 404, the other never     -> COURSE. The facility really has
                                                no searchable inventory.

Read only, ~28 requests, and the slow phase is gentler than a person clicking
through dates. It prints which of the two it is.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, ".")

from scraper.adapters.base import USER_AGENT          # noqa: E402

PING_JS = r"""
async ([bodyStr, dateStr]) => {
  let body;
  try { body = JSON.parse(bodyStr); } catch (e) { return {error: "bad predicate"}; }
  body.date = dateStr;
  let r, text;
  try {
    r = await fetch(location.origin + "/api/tee-times/tee-time-search-results",
      {method:"POST", headers:{"Content-Type":"application/json","Accept":"application/json"},
       body: JSON.stringify(body)});
    text = await r.text();
  } catch (e) { return {error: "fetch: " + String(e)}; }
  const out = {status: r.status, bytes: text.length};
  try {
    const j = JSON.parse(text);
    out.total = ((j.ttResults && j.ttResults.teeTimes) || []).length;
  } catch (e) { out.not_json = true; }
  return out;
}
"""


def runs_of(seq: list[bool]) -> list[int]:
    """Lengths of consecutive same-value stretches — clumping, quantified.

    [T,T,F,F,F,T] -> [2,3,1]. A coin flip gives mostly 1s and 2s; a throttle
    gives long runs. This is the whole statistic the answer turns on, so it is
    a plain function with no state to get wrong.
    """
    out: list[int] = []
    prev = None
    for i, v in enumerate(seq):
        if i and v == prev:
            out[-1] += 1
        else:
            out.append(1)
        prev = v
    return out


def interpret(hits: list[dict], names: tuple[str, str]) -> str:
    good, bad = names
    per = {n: [h for h in hits if h["who"] == n] for n in names}
    rate = {n: sum(1 for h in v if h.get("status") == 200) / max(len(v), 1)
            for n, v in per.items()}
    seq = [h.get("status") == 200 for h in hits]
    lens = runs_of(seq)
    clumped = max(lens) >= 3 and len(lens) < len(seq) * 0.7
    if rate[good] > 0.9 and rate[bad] < 0.1:
        return (f"COURSE. {good} answered {rate[good]:.0%} of the time and {bad} "
                f"{rate[bad]:.0%}, interleaved in the same session at the same "
                "spacing. The facility is the difference — GolfNow has nothing "
                "searchable under that id, and no amount of retrying changes it.")
    if clumped:
        return (f"RATE. Outcomes come in runs of {lens} rather than tracking the "
                f"course ({good} {rate[good]:.0%}, {bad} {rate[bad]:.0%}). Both "
                "courses fail together and recover together, so this is a "
                "throttle answering 404. The adapter needs to back off across "
                "facilities, not retry harder within one.")
    return (f"UNCLEAR — {good} {rate[good]:.0%}, {bad} {rate[bad]:.0%}, runs "
            f"{lens}. Neither explanation is clean; do not act on this yet.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", default="probe-results/golfnow-diag.json")
    ap.add_argument("--good", default="cedaredge-golf-club-golfnow")
    ap.add_argument("--bad", default="clubcorp-at-black-bear-golf-club")
    ap.add_argument("--out", default="probe-results/golfnow-flap.txt")
    a = ap.parse_args()

    diag = {r["slug"]: r for r in json.loads(pathlib.Path(a.diag).read_text())}
    for s in (a.good, a.bad):
        if s not in diag or not diag[s].get("predicate"):
            print(f"no recorded predicate for {s}")
            return 2
    d = dt.date.today() + dt.timedelta(days=1)
    date_str = f"{d:%b} {d.day} {d:%Y}"
    plan = [(3000, 20), (20000, 8)]   # (gap ms, count) — fast phase, slow phase

    from playwright.sync_api import sync_playwright
    hits: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(diag[a.good]["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(9000)
        for gap, count in plan:
            print(f"\n-- {count} requests, {gap/1000:g}s apart --", flush=True)
            for i in range(count):
                who = a.good if i % 2 == 0 else a.bad
                try:
                    r = page.evaluate(PING_JS, [diag[who]["predicate"], date_str])
                except Exception as e:  # noqa: BLE001
                    r = {"error": f"{type(e).__name__}: {e}"[:100]}
                r.update({"who": who, "gap_ms": gap, "i": len(hits)})
                hits.append(r)
                print(f"   {who:<40} {r.get('status')} total={r.get('total')}",
                      flush=True)
                page.wait_for_timeout(gap)
        browser.close()

    answer = interpret(hits, (a.good, a.bad))
    lines = [f"golfnow flap test — {dt.datetime.now(dt.timezone.utc).isoformat()}",
             f"page  : {diag[a.good]['url']}",
             f"good  : {a.good}", f"bad   : {a.bad}", f"date  : {date_str}", "",
             f"ANSWER: {answer}", ""]
    for gap, _ in plan:
        ph = [h for h in hits if h["gap_ms"] == gap]
        lines.append(f"  -- {gap/1000:g}s apart --")
        for h in ph:
            lines.append(f"    {h['who']:<40} status={h.get('status')} "
                         f"total={h.get('total')} bytes={h.get('bytes')}"
                         + (f" error={h['error']}" if h.get("error") else ""))
        for n in (a.good, a.bad):
            v = [h for h in ph if h["who"] == n]
            ok = sum(1 for h in v if h.get("status") == 200)
            lines.append(f"      {n}: {ok}/{len(v)} answered")
        lines.append("")
    p = pathlib.Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))
    p.with_suffix(".json").write_text(json.dumps(hits, indent=1))
    print(f"\nANSWER: {answer}\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
