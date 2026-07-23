"""Diagnostic #2: capture a POPULATED GolfNow tee-time slot's fields.

Probe #1 found the list endpoint — POST /api/tee-times/tee-time-search-results
-> {ttResults:{teeTimes:[...]}} — but the page defaulted to today (sold out this
late), so teeTimes came back empty and we never saw a slot's shape. Here we grab
the page's own real search body, replay it in-page with the date pushed to
tomorrow (and a couple days out as backup) and a big pageSize, then print the
count and the FULL first slot so we learn exact field names (time, price, holes,
players, booking url).
"""
from __future__ import annotations

import datetime as dt
import json

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FACILITIES = [("453", "arrowhead-golf-club")]
SEARCH_EP = "/api/tee-times/tee-time-search-results"

# Replay the captured body with date + pageSize overridden, for several dates
# until one returns slots. Returns count + first slot (full) + min rate.
REPLAY_JS = r"""
async ([bodyStr, dates]) => {
  const base = location.origin;
  const out = [];
  for (const d of dates) {
    let body;
    try { body = JSON.parse(bodyStr); } catch (e) { return {error:"bad body"}; }
    body.date = d;
    body.pageSize = 40;
    body.teeTimeCount = 40;
    const r = await fetch(base + "/api/tee-times/tee-time-search-results",
      {method:"POST", headers:{"Content-Type":"application/json","Accept":"application/json"},
       body:JSON.stringify(body)});
    let j = {}; try { j = await r.json(); } catch (e) {}
    const tt = (j.ttResults && j.ttResults.teeTimes) || [];
    let slim = null, keys = null;
    if (tt[0]) {
      keys = Object.keys(tt[0]);
      slim = {};
      for (const k of keys) {
        const v = tt[0][k];
        // drop the huge facility blob; keep tee-time scalar fields verbatim
        if (k === "facility") { slim[k] = "<facility obj omitted>"; continue; }
        slim[k] = v;
      }
    }
    out.push({date:d, status:r.status, count:tt.length,
              keys, slim,
              minRate: (j.ttResults && j.ttResults.minDailyTeeTimeRate &&
                        j.ttResults.minDailyTeeTimeRate.formattedValue) || null});
    if (tt.length) break;
  }
  return {tries: out};
}
"""


def probe(pw, fid: str, slug: str, dates: list[str]) -> None:
    b = pw.chromium.launch(args=["--no-sandbox"])
    captured: dict[str, str] = {}
    try:
        pg = b.new_page(user_agent=UA)

        def on_request(req):
            if SEARCH_EP in req.url and req.method == "POST":
                try:
                    captured["body"] = req.post_data or ""
                except Exception:
                    pass

        pg.on("request", on_request)
        pg.goto(f"https://www.golfnow.com/tee-times/facility/{fid}-{slug}/search",
                wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(12000)  # let the page fire its own search

        if "body" not in captured:
            print(f"RESULT gn2 {fid}: no search body captured", flush=True)
            return
        r = pg.evaluate(REPLAY_JS, [captured["body"], dates])
        print(f"RESULT gn2 {fid}-{slug}:", flush=True)
        print("  captured_body:", captured["body"][:1200], flush=True)
        for t in (r.get("tries") or []):
            print(f"  date={t['date']} status={t['status']} count={t['count']} "
                  f"minRate={t.get('minRate')}", flush=True)
            if t.get("keys"):
                print("    KEYS:", json.dumps(t["keys"]), flush=True)
            if t.get("slim"):
                print("    SLOT:", json.dumps(t["slim"])[:2500], flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT gn2 {fid}: ERROR {type(e).__name__} {str(e)[:120]}", flush=True)
    finally:
        b.close()


def main() -> None:
    from playwright.sync_api import sync_playwright
    dates = [(dt.date.today() + dt.timedelta(days=n)).strftime("%b %d %Y")
             for n in (1, 2, 3)]
    with sync_playwright() as pw:
        for fid, slug in FACILITIES:
            probe(pw, fid, slug, dates)


if __name__ == "__main__":
    main()
