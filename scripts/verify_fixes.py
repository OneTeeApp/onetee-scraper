"""#69 — measure the three shipped fixes end to end, before vs after.

Every claim in issues #66-#68 is currently "the code looks right". This runs
the OLD code path and the NEW one against the SAME live sheets on the SAME
dates and prints both counts, so the gain is measured rather than argued.

  A. TeeItUp pinned facilities (commit a248c79)
     BEFORE: the old prologue's first call passed the pinned facility_id
     straight through as ?facilityIds=... . kenna answers HTTP 500 to that
     param on every alias as of 2026-07 (diag4 section E), so all ten pinned
     courses failed on every scrape. This reproduces that exact call.
     AFTER:  the bare per-alias call, filtered client-side.

  B. Teesnap course discovery (commit 8a5f491)
     BEFORE: ids came from a regex anchored on `"id":<n>,"created_at"` anywhere
     near window.courses, which swept up each course's embedded PROPERTY id;
     Hollydot's property 1329 and Petteys' 1081 answer HTTP 500, and the old
     fetch raised as soon as one id failed, so the whole venue returned nothing.
     This reproduces the old id list and the old raise-on-any-failure rule.
     AFTER:  top-level JSON entries only, and raise only if EVERY id failed.

  C. Arizona native re-tags (commit 6c9027c)
     BEFORE: these eight slugs had no native row at all — they were GolfNow-only,
     so native capture was 0 by construction. Nothing to re-run; the number
     that matters is what the native adapters return now, per registry.

Report only. Nothing here edits the CSV, the registry, or D1.
Public endpoints, no credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teeitup import TeeItUpAdapter  # noqa: E402
from scraper.adapters.teesnap import TeesnapAdapter  # noqa: E402
from scraper.aggregate import ADAPTERS, load_registry  # noqa: E402

REG = "registry.json"
DATES = [dt.date.today() + dt.timedelta(days=d) for d in (1, 3, 7)]

# The eight Arizona rows moved off GolfNow in 6c9027c.
AZ_RETAGGED = [
    "painted-mountain-golf-resort",
    "hillcrest-golf-club",
    "continental-country-club",
    "chaparral-golf-country-club",
    "dave-white-municipal-golf-course",
    "union-hills-golf-club",
    "agave-highlands-golf-course",
    "forty-niner-country-club",
]


class OldTeesnap(TeesnapAdapter):
    """The pre-8a5f491 behaviour, reproduced so the gain can be measured."""

    def discover_courses_old(self, sub: str) -> list[int]:
        html = self._get_text(f"https://{sub}.teesnap.net/")
        start = html.find("window.courses")
        region = html[start:start + 30000] if start >= 0 else html
        ids: list[str] = []
        for i in re.findall(r'"id":\s*(\d+)\s*,\s*"created_at"', region):
            if i not in ids:
                ids.append(i)
        return [int(i) for i in ids]

    def fetch_old(self, course: dict, date: dt.date) -> list:
        """Old rule: the FIRST failing id kills the whole venue."""
        sub = course["ids"]["subdomain"]
        ids = self.discover_courses_old(sub)
        if not ids:
            raise RuntimeError("no Teesnap course id in window.courses")
        n = 0
        for cid in ids:
            data = self.get_json(          # no try/except — that is the point
                f"https://{sub}.teesnap.net/customer-api/teetimes-day",
                params={"course": cid, "date": date.isoformat(),
                        "players": 1, "holes": 18, "addons": "off"})
            block = (data or {}).get("teeTimes", {})
            for slot in block.get("teeTimes", []):
                secs = [s for s in (slot.get("teeOffSections") or [])
                        if (s.get("turnTo") or {}).get("time") or s.get("time")]
                n += len(secs) or (1 if slot.get("teeTime") else 0)
        return [None] * n


def by_slug(reg: list[dict]) -> dict[str, dict]:
    return {c["slug"]: c for c in reg}


def count(fn) -> tuple[int | None, str]:
    """-> (slot count, note). None means the call raised."""
    try:
        return len(fn()), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:110]}"


def section_a(reg: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("A. TeeItUp pinned facilities — old facilityIds call vs new bare call")
    print("=" * 72)
    rows = [c for c in reg
            if c["platform"] == "teeitup" and c["ids"].get("facility_id")]
    print(f"{len(rows)} pinned rows in the registry\n")
    ad = TeeItUpAdapter()
    tot_before = tot_after = 0
    for c in sorted(rows, key=lambda r: r["slug"]):
        alias, fid = c["ids"]["alias"], c["ids"]["facility_id"]
        print(f"--- {c['slug']}  alias={alias} facility_id={fid}")
        def old_call(d: dt.date) -> list:
            """Exactly what the old prologue did first: pin facilityIds."""
            data = ad._teetimes(alias, d, fid)
            blocks = data if isinstance(data, list) else [data]
            return [s for b in blocks
                    for s in ((b or {}).get("teetimes", []) or [])]

        for date in DATES:
            before, bnote = count(lambda d=date: old_call(d))
            after, anote = count(lambda d=date: ad.fetch(c, d))
            tot_before += before or 0
            tot_after += after or 0
            print(f"    {date}  BEFORE {before if before is not None else 'RAISED'}"
                  f"{' (' + bnote + ')' if bnote else ''}")
            print(f"    {date}  AFTER  {after if after is not None else 'RAISED'}"
                  f"{' (' + anote + ')' if anote else ''}")
        sys.stdout.flush()
    print(f"\nA TOTAL over {len(rows)} courses x {len(DATES)} dates: "
          f"before={tot_before}  after={tot_after}  "
          f"delta=+{tot_after - tot_before}")


def section_b(reg: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("B. Teesnap discovery — old regex + raise-on-any vs new JSON parse")
    print("=" * 72)
    rows = [c for c in reg if c["platform"] == "teesnap"]
    print(f"{len(rows)} Teesnap rows in the registry\n")
    old, new = OldTeesnap(), TeesnapAdapter()
    tot_before = tot_after = 0
    for c in sorted(rows, key=lambda r: r["slug"]):
        sub = c["ids"]["subdomain"]
        print(f"--- {c['slug']}  {sub}.teesnap.net")
        try:
            old_ids = old.discover_courses_old(sub)
        except Exception as exc:  # noqa: BLE001
            old_ids = []
            print(f"    old id scan raised: {type(exc).__name__}: {str(exc)[:90]}")
        try:
            new_ids = new.discover_courses(sub)
        except Exception as exc:  # noqa: BLE001
            new_ids = []
            print(f"    new id scan raised: {type(exc).__name__}: {str(exc)[:90]}")
        print(f"    old ids: {old_ids}")
        print(f"    new ids: {[(d['id'], d.get('name')) for d in new_ids]}")
        bogus = sorted(set(old_ids) - {d["id"] for d in new_ids})
        if bogus:
            print(f"    ids the old regex invented (property ids): {bogus}")
        for date in DATES:
            before, bnote = count(lambda d=date: old.fetch_old(c, d))
            after, anote = count(lambda d=date: new.fetch(c, d))
            tot_before += before or 0
            tot_after += after or 0
            print(f"    {date}  BEFORE {before if before is not None else 'RAISED'}"
                  f"{' (' + bnote + ')' if bnote else ''}"
                  f"   AFTER {after if after is not None else 'RAISED'}"
                  f"{' (' + anote + ')' if anote else ''}")
        sys.stdout.flush()
    print(f"\nB TOTAL over {len(rows)} courses x {len(DATES)} dates: "
          f"before={tot_before}  after={tot_after}  "
          f"delta=+{tot_after - tot_before}")


def section_c(reg: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("C. Arizona re-tags — native capture that did not exist before")
    print("=" * 72)
    idx = by_slug(reg)
    total = 0
    for slug in AZ_RETAGGED:
        c = idx.get(slug)
        if not c:
            print(f"--- {slug}: NOT IN REGISTRY (re-tag did not land)")
            continue
        print(f"--- {c['name']}  [{c['platform']}] {c['ids']}  "
              f"role={c.get('source_role')} status={c.get('status')}")
        sup = [x for x in reg if x.get("venue_id") == c.get("venue_id")
               and x["slug"] != slug]
        print(f"    venue {c.get('venue_id')} also carries: "
              f"{[(s['slug'], s['platform'], s.get('source_role')) for s in sup] or 'nothing'}")
        ad = ADAPTERS[c["platform"]]()
        for date in DATES:
            n, note = count(lambda d=date: ad.fetch(c, d))
            total += n or 0
            print(f"    {date}  BEFORE 0 (no native row existed)   "
                  f"AFTER {n if n is not None else 'RAISED'}"
                  f"{' (' + note + ')' if note else ''}")
        sys.stdout.flush()
    print(f"\nC TOTAL over {len(AZ_RETAGGED)} courses x {len(DATES)} dates: "
          f"before=0  after={total}  delta=+{total}")


def main() -> None:
    print("verify_fixes (#69): before/after for the three shipped fixes")
    print(f"dates: {', '.join(d.isoformat() for d in DATES)}")
    print("Report only. Nothing here edits the CSV, the registry, or D1.")
    reg = load_registry(REG)
    print(f"registry: {len(reg)} booking sources")
    for fn in (section_a, section_b, section_c):
        try:
            fn(reg)
        except Exception:  # noqa: BLE001
            print(f"    HARNESS ERROR in {fn.__name__}:")
            traceback.print_exc(limit=3)
        sys.stdout.flush()
    print("\ndone")


if __name__ == "__main__":
    main()
