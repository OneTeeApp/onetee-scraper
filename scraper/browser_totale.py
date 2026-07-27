"""Headless-browser fetcher for Total-e-Integrated (*.totaleintegrated.com).

The Sun City West seven and Ken McDonald book on Total-e-Integrated, a DNN
(ASP.NET WebForms) app. There is no clean JSON API: the public tee sheet is a
`/Public-Tee-Times/Tee-Times` page whose date is advanced by __doPostBack against
an UpdatePanel, and — critically — the selected date lives in the ENCRYPTED
__VIEWSTATE, not in any client field. Measured: replaying the working postback
with an overridden `hdnCalSelected` returns the date baked into the viewstate,
not the override, so a plain-HTTP requests client cannot drive the date. A real
browser can: the day-strip cells are click-delegated and the framework carries
the viewstate for us.

SHAPE (measured from suncitywest, 2026-07-27):
- ONE tee sheet per tenant lists ALL of that tenant's courses interleaved, each
  `.TeeBlock` card labelled with its course ("Echo Mesa", "Trail Ridge", ...).
  So one page load + one click per day yields every course for that day.
- Booking window is short (~5 days): the day strip shows today..+4 and the
  calendar disables the rest. There is no far horizon here — we harvest whatever
  the strip publishes, which is the whole window.
- A card carries date, time, course, an optional "$N/Player" price, a
  "Players min - max" range (max = seats still bookable = open_spots), and
  "Holes 9 18". Times are already course-local (Phoenix); no tz math.

OWNERSHIP: this owns ALL `totale` courses; the plain tiers exclude the platform,
so the two never write the same course_slug+date. Emits one aggregate-format
JSON per harvested date for `scraper.d1 push`.

Usage:
    python -m scraper.browser_totale --registry registry.json --out-dir output
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import pathlib
import re
import sys

from .adapters.base import USER_AGENT
from .adapters.experimental import TotaleAdapter
from .aggregate import load_registry

log = logging.getLogger("teetime")

TENANT_URL = "https://{tenant}.totaleintegrated.com/Public-Tee-Times/Tee-Times"

# Click through every day cell in the strip and return parsed rows for all of
# them. Runs in the page so the DNN postback/viewstate is handled natively.
HARVEST_JS = r"""
async () => {
  const norm = s => (s || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();
  function parseBlocks() {
    const out = [];
    for (const b of document.querySelectorAll(".TeeBlock")) {
      const txt = norm(b.textContent);
      const date = (txt.match(/(\d{2}\/\d{2}\/\d{4})/) || [])[1];
      const time = (txt.match(/(\d{1,2}:\d{2}\s?[AP]M)/i) || [])[1];
      if (!date || !time) continue;
      const rest = txt.slice(txt.indexOf(time) + time.length);
      const course = norm(rest.split(/\$|Players/)[0]);
      const price = (rest.match(/\$(\d+(?:\.\d+)?)\s*\/\s*Player/i) || [])[1];
      const range = rest.match(/Players\s*(\d+)\s*-\s*(\d+)/i);
      const openSpots = range ? parseInt(range[2], 10) : null;
      const holesM = rest.match(/Holes\s*([0-9 ]+?)\s*(BOOK|$)/i);
      const holes = holesM ? [...holesM[1].matchAll(/\d+/g)]
        .map(x => +x[0]).filter(h => h === 9 || h === 18) : [];
      out.push({ date, time, course, price: price ? +price : null, openSpots, holes });
    }
    return out;
  }
  const seen = {};           // date -> rows
  const add = rows => { for (const r of rows) { (seen[r.date] = seen[r.date] || []).push(r); } };

  // day-strip cells look like "28 Jul"; the current day is already rendered.
  const labelsOf = () => [...new Set([...document.querySelectorAll("div")]
    .map(d => norm(d.textContent))
    .filter(t => /^\d{1,2}\s+[A-Za-z]{3}$/.test(t)))];
  add(parseBlocks());
  const labels = labelsOf();
  for (const lbl of labels) {
    // find a visible strip cell with exactly this label and click it
    const cell = [...document.querySelectorAll("div")]
      .find(d => norm(d.textContent) === lbl && d.offsetParent);
    if (!cell) continue;
    cell.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    // wait for the sheet to reflect this label's date (or give up after ~6s)
    const want = lbl.replace(/^(\d{1,2})\s+([A-Za-z]{3}).*/, "$1 $2");
    let ok = false;
    for (let i = 0; i < 24; i++) {
      await new Promise(r => setTimeout(r, 250));
      const b = document.querySelector(".TeeBlock");
      if (b && new RegExp("\\b" + want.split(" ")[0] + "\\b").test(norm(b.textContent))
          && norm(b.textContent).includes(want.split(" ")[1])) { ok = true; break; }
    }
    add(parseBlocks());
  }
  // de-dupe rows within each date (course+time+holes)
  const clean = {};
  for (const d of Object.keys(seen)) {
    const m = new Map();
    for (const r of seen[d]) m.set(r.course + "|" + r.time + "|" + r.holes.join(","), r);
    clean[d] = [...m.values()];
  }
  return clean;
}
"""

_NORM = re.compile(r"[^a-z0-9]+")


def _key(s: str) -> str:
    return _NORM.sub("", (s or "").lower())


def _iso(date_mdy: str, time_ampm: str) -> str | None:
    """'07/28/2026' + '6:37 AM' -> '2026-07-28T06:37:00' (already local)."""
    try:
        d = dt.datetime.strptime(date_mdy, "%m/%d/%Y").date()
        t = dt.datetime.strptime(time_ampm.upper().replace(" ", ""), "%I:%M%p").time()
        return dt.datetime.combine(d, t).isoformat(timespec="seconds")
    except ValueError:
        return None


def run(registry_path: str, out_dir: str) -> dict:
    from playwright.sync_api import sync_playwright

    registry = load_registry(registry_path)
    courses = [c for c in registry
               if c["platform"] == "totale" and c["ids"].get("tenant")]
    tenants: dict[str, list] = {}
    for c in courses:
        tenants.setdefault(c["ids"]["tenant"], []).append(c)
    log.info("browser-fetching %d totale courses across %d tenants",
             len(courses), len(tenants))

    # date -> list[TeeTime]
    by_date: dict[str, list] = {}
    errors: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for tenant, tcourses in tenants.items():
            # label -> course; if a tenant has one course, everything maps to it
            by_label = {_key(c["ids"].get("label") or c["name"]): c for c in tcourses}
            single = tcourses[0] if len(tcourses) == 1 else None
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                page.goto(TENANT_URL.format(tenant=tenant),
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)     # let the initial sheet render
                harvested = page.evaluate(HARVEST_JS)
            except Exception as e:  # noqa: BLE001
                errors.append({"tenant": tenant, "error": f"{type(e).__name__}: {e}"})
                log.info("  tenant %-22s ERROR %s", tenant, type(e).__name__)
                page.close()
                continue
            page.close()

            kept = matched = 0
            for date_mdy, rows in (harvested or {}).items():
                for r in rows:
                    course = by_label.get(_key(r.get("course"))) or single
                    if not course:
                        continue        # a sibling course not in our registry
                    teetime = _iso(date_mdy, r.get("time") or "")
                    if not teetime:
                        continue
                    price = r.get("price")
                    tt = TotaleAdapter.base_tee_time(
                        course,
                        teetime=teetime,
                        holes=r.get("holes") or [],
                        open_spots=r.get("openSpots"),
                        price_min=price, price_max=price,
                        raw=r,
                    )
                    iso_date = teetime[:10]
                    by_date.setdefault(iso_date, []).append(tt)
                    kept += 1
                    matched += 1
            log.info("  tenant %-22s %d tee times over %d dates",
                     tenant, kept, len(harvested or {}))

    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for iso_date, tts in sorted(by_date.items()):
        doc = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date": iso_date,
            "courses_queried": len(courses),
            "courses_ok": len({t.course_slug for t in tts}),
            "tee_times": [t.to_dict() for t in tts],
            "errors": errors,
        }
        p = out / f"totale_{iso_date}.json"
        p.write_text(json.dumps(doc, indent=2))
        written.append(str(p))
        log.info("wrote %s (%d tee times)", p, len(tts))

    log.info("totale browser: %d dates, %d total tee times, %d errors",
             len(written), sum(len(v) for v in by_date.values()), len(errors))
    return {"dates": len(written), "files": written, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-based Total-e-Integrated fetcher")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--out-dir", default="output")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    run(a.registry, a.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
