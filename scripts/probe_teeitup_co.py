"""Why do three Colorado TeeItUp courses return nothing when twenty-two work?

WHY
---
Colorado has 25 TeeItUp registry rows. Twenty-two of them serve tee times with
no `facility_id` pinned at all — the bare per-alias call is enough. Exactly
three are silent, and they are the same three sitting in the Colorado coverage
gap:

    trinidad-golf-course          alias trinidad-municipal-golf-course
    golf-granby-ranch             alias golf-granby-ranch
    rollingstone-ranch-golf-club  alias rollingstone-ranch

That ratio is the whole reason this probe exists. A platform-wide fault would
not spare 22 of 25, so the fault is per-course: a wrong alias, a host on a
different SPA generation, or a sheet that genuinely needs the `facilityIds`
parameter the other 22 do not.

THE LEAD
--------
Trinidad's live booking URL, supplied by a human who loaded it in a browser:

    https://trinidad-municipal-golf-course.book-v2.teeitup.golf/
        ?course=13600&date=2026-07-27&max=999999

Two things in there we do not have. The host is **book-v2**, not `.book.` —
a different SPA generation, which may or may not sit on the same kenna
backend. And `course=13600` is an integer facility id that the registry does
not carry (`ids.facility_id` is None for all 25 CO rows).

13600 being the id `facilityIds` wants is a HYPOTHESIS, not a fact. The SPA's
query parameter and the API's parameter have never been shown to be the same
number, and teeitup.py already documents one place where two plausible
identities for the same course are NOT interchangeable (the integer `id` vs
the Mongo `courseId` — comparing the wrong one deleted 4487 slots in a248c79).
So this probe tests the pin rather than assuming it, and it tests it against a
control alias that is known to work.

WHAT IT ANSWERS, KEPT APART
---------------------------
Same discipline as the other probes: a missing answer is never recorded as a
known one.

  facilities   does /alias/<alias>/facilities resolve, and what integer ids,
               Mongo courseIds and timeZone does it list? An alias kenna does
               not know 404s here — that alone explains a silent course, and
               it is a different repair (fix the alias) from a missing pin.
  sheet        /v2/tee-times across three spread dates, tried bare AND with
               each candidate pin, so "empty without the pin, full with it"
               is visible as a difference rather than inferred.
  fetch        the REAL fetch_course() path, the same one the hourly scan
               calls, so the verdict is about production and not about this
               script.
  page         the booking host's HTML, checked only for which API host and
               alias the bundle names. If book-v2 talks to something other
               than phx-api-be-east-1b.kenna.io, every kenna result above is
               answering the wrong question, and that has to be visible.

Three dates rather than one: a single closed day, a tournament or a frost
delay is not a dead course.

Report only. Public GETs, no credentials, no D1 writes, no registry edits, no
CAPTCHA solving, no TLS fingerprinting. It runs in CI because this repo's dev
sandbox egress-proxies 403 both the platforms and the Worker API.

  python3 scripts/probe_teeitup_co.py [--out probe-results/teeitup-co.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teeitup import API_BASE, TeeItUpAdapter  # noqa: E402
from scraper.aggregate import fetch_course, load_registry  # noqa: E402

# The three silent CO aliases, plus controls. Controls are not decoration:
# without them a fleet-wide kenna 429 during this run would read as three dead
# courses. `vail-golf-club` is a plain serving alias; `riverdale` serves and is
# a multi-course facility, so its facilities list shows what a populated one
# looks like.
TARGETS = [
    {"slug": "trinidad-golf-course", "alias": "trinidad-municipal-golf-course",
     "role": "target", "pins": ["13600"],
     "page": "https://trinidad-municipal-golf-course.book-v2.teeitup.golf/"
             "?course=13600&date={date}&max=999999"},
    {"slug": "golf-granby-ranch", "alias": "golf-granby-ranch",
     "role": "target", "pins": [],
     "page": "https://golf-granby-ranch.book.teeitup.golf/"},
    {"slug": "rollingstone-ranch-golf-club", "alias": "rollingstone-ranch",
     "role": "target", "pins": [],
     "page": "https://rollingstone-ranch.book.teeitup.golf/"},
    {"slug": "vail-golf-club", "alias": "vail-golf-club",
     "role": "control", "pins": [], "page": ""},
    {"slug": "riverdale-golf-course", "alias": "riverdale",
     "role": "control", "pins": [], "page": ""},
]

# Spread so one closed day cannot look like a dead course.
DATE_OFFSETS = (1, 3, 7)

# What we look for in the SPA bundle. If book-v2 names a different API host,
# every kenna answer in this report is about the wrong backend.
HOST_RE = re.compile(r"https://[a-z0-9.\-]*(?:kenna\.io|teeitup\.[a-z]+)"
                     r"(?:/[a-zA-Z0-9/_\-]*)?")
ALIAS_RE = re.compile(r"(?:x-be-alias|beAlias|alias)['\"]?\s*[:=]\s*"
                      r"['\"]([a-z0-9\-]+)['\"]", re.I)


def probe_route(ad: TeeItUpAdapter, url: str, alias: str) -> dict:
    """One discovery call. Status, shape and ids — errors kept verbatim."""
    try:
        r = ad.session.get(url, headers={"x-be-alias": alias}, timeout=30)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if r.status_code != 200:
        return {"ok": False, "http": r.status_code, "body": r.text[:200]}
    try:
        data = r.json()
    except Exception:                                          # noqa: BLE001
        return {"ok": False, "http": 200,
                "error": f"non-JSON ({len(r.text)}B)"}
    rows = data if isinstance(data, list) else data.get("courses", [])
    return {"ok": True, "http": 200, "count": len(rows),
            "facilities": [{"id": f.get("id"),
                            "courseId": f.get("courseId"),
                            "name": f.get("name"),
                            "timeZone": f.get("timeZone")}
                           for f in rows if isinstance(f, dict)]}


def probe_sheet(ad: TeeItUpAdapter, alias: str, date: dt.date,
                pin: str | None) -> dict:
    """One /v2/tee-times call. Empty and error are NEVER folded together."""
    try:
        data = ad._teetimes(alias, date, pin)
    except Exception as exc:                                   # noqa: BLE001
        return {"result": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    blocks = data if isinstance(data, list) else [data]
    slots = [s for b in blocks for s in ((b or {}).get("teetimes", []) or [])]
    cids = sorted({str(s.get("courseId")) for s in slots if s.get("courseId")})
    return {"result": "rows" if slots else "empty",
            "slots": len(slots),
            "course_ids": cids[:6],
            "first": (json.dumps(slots[0])[:200] if slots else None)}


def probe_page(ad: TeeItUpAdapter, url: str) -> dict:
    """Which backend and alias does the booking host's own bundle name?"""
    try:
        r = ad.session.get(url, timeout=30)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    hosts = sorted({m.split("/")[2] for m in HOST_RE.findall(r.text)})
    return {"ok": r.status_code == 200, "http": r.status_code,
            "bytes": len(r.text), "final_url": str(r.url),
            "api_hosts": hosts[:12],
            "aliases_named": sorted(set(ALIAS_RE.findall(r.text)))[:8]}


def verdict_for(rec: dict) -> str:
    """One target's record -> what this run actually established.

    ORDER IS THE POINT. A run where every call raised establishes NOTHING
    about any alias; read as "kenna does not know this club" it would condemn
    the working controls too, which is exactly what the controls are here to
    catch. So unreachable is tested FIRST, and `alias_unknown_to_kenna` is
    asserted only on a real HTTP 404 from the discovery route — never on a
    transport failure, and never by inference from an empty sheet.

      unreachable_this_run   every bare call raised. Says nothing. Re-run.
      alias_unknown_to_kenna discovery answered 404: the alias is wrong.
      discovery_failed       discovery did not answer and no pin helped;
                             unknown, deliberately distinct from 404.
      needs_pin:<id>         bare is empty, this pin returns rows. The repair
                             is one registry field.
      serving_bare           the bare call works — the silence is downstream
                             of this adapter, not in it.
      empty_everywhere       every route answered cleanly and returned zero.
                             Not a pass and not a failure: seasonal, closed,
                             or genuinely dark, and a human has to look.
    """
    sheets = rec.get("sheets", {})
    bare = sheets.get("(bare)", {})
    bare_rows = sum(v.get("slots", 0) for v in bare.values())
    bare_err = all(v.get("result") == "error"
                   for v in bare.values()) if bare else False
    winners = [k for k, v in sheets.items()
               if k != "(bare)" and sum(x.get("slots", 0)
                                        for x in v.values()) > 0]
    fr = rec.get("facilities_route") or {}

    if bare_err and not winners:
        return "unreachable_this_run"
    if fr.get("http") == 404 and not winners:
        return "alias_unknown_to_kenna"
    if not fr.get("ok") and not winners and not bare_rows:
        return "discovery_failed"
    if winners and not bare_rows:
        return f"needs_pin:{winners[0]}"
    if bare_rows:
        return "serving_bare"
    return "empty_everywhere"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="probe-results/teeitup-co.json")
    ap.add_argument("--registry", default="registry.json")
    a = ap.parse_args()

    today = dt.date.today()
    dates = [today + dt.timedelta(days=d) for d in DATE_OFFSETS]
    ad = TeeItUpAdapter()
    reg = {c["slug"]: c for c in load_registry(a.registry)}

    print("probe_teeitup_co: why three CO aliases return nothing while 22 work")
    print(f"dates: {', '.join(d.isoformat() for d in dates)}")
    print("Report only. No writes to D1, the registry or the CSV.\n")

    out: dict = {"generated_at": dt.datetime.now(dt.timezone.utc)
                 .isoformat(timespec="seconds"),
                 "dates": [d.isoformat() for d in dates],
                 "api_base": API_BASE,
                 "targets": []}

    for t in TARGETS:
        rec: dict = {"slug": t["slug"], "alias": t["alias"], "role": t["role"]}
        print("=" * 72)
        print(f"{t['slug']}  ({t['role']})   alias={t['alias']}")
        print("=" * 72)

        # 1. discovery ------------------------------------------------------
        rec["facilities_route"] = probe_route(
            ad, f"{API_BASE}/alias/{t['alias']}/facilities", t["alias"])
        rec["v2_courses_route"] = probe_route(
            ad, f"{API_BASE}/v2/courses", t["alias"])
        fr = rec["facilities_route"]
        if fr.get("ok"):
            print(f"  facilities: {fr['count']} listed")
            for f in fr["facilities"][:8]:
                print(f"      id={str(f['id']):<8} courseId={str(f['courseId'])[:26]:<26} "
                      f"tz={str(f['timeZone'] or ''):<20} {f['name']}")
        else:
            print(f"  facilities: FAILED {fr.get('http') or ''} "
                  f"{fr.get('error') or fr.get('body','')[:120]}")
        v2 = rec["v2_courses_route"]
        print(f"  /v2/courses: {'ok ' + str(v2.get('count')) if v2.get('ok') else 'no'}")
        sys.stdout.flush()

        # Candidate pins: whatever the human-supplied URL carried, plus every
        # integer id discovery listed. Tried separately, never merged — the
        # point is to see WHICH one produces a sheet.
        discovered = [str(f["id"]) for f in fr.get("facilities", [])
                      if f.get("id") is not None]
        pins: list[str | None] = [None] + [p for p in t["pins"]] + \
                                 [d for d in discovered if d not in t["pins"]]

        # 2. sheets ---------------------------------------------------------
        rec["sheets"] = {}
        for pin in pins[:5]:
            key = pin or "(bare)"
            rec["sheets"][key] = {}
            for d in dates:
                res = probe_sheet(ad, t["alias"], d, pin)
                rec["sheets"][key][d.isoformat()] = res
                print(f"  sheet pin={key:<10} {d.isoformat()}  "
                      f"{res['result']:<6} {res.get('slots', 0):>4} slots"
                      + (f"   {res['error']}" if res.get("error") else ""))
                sys.stdout.flush()

        # 3. the real production path ---------------------------------------
        course = reg.get(t["slug"])
        rec["fetch_course"] = {}
        if course is None:
            rec["fetch_course"] = {"error": "slug not in registry"}
            print("  fetch_course: slug not in registry")
        else:
            for d in dates:
                res = fetch_course(course, d)
                rec["fetch_course"][d.isoformat()] = (
                    {"result": "rows" if res.tee_times else "empty",
                     "slots": len(res.tee_times)} if res.ok
                    else {"result": "error", "error": str(res.error)[:300]})
                r = rec["fetch_course"][d.isoformat()]
                print(f"  fetch_course        {d.isoformat()}  "
                      f"{r['result']:<6} {r.get('slots', 0):>4} slots"
                      + (f"   {r['error'][:120]}" if r.get("error") else ""))
                sys.stdout.flush()

        # 4. what the booking host itself says -------------------------------
        if t["page"]:
            page_url = t["page"].format(date=dates[0].isoformat())
            rec["page"] = probe_page(ad, page_url)
            p = rec["page"]
            print(f"  page {page_url}")
            print(f"      HTTP {p.get('http')}  {p.get('bytes')}B  "
                  f"final={p.get('final_url')}")
            print(f"      api hosts named: {p.get('api_hosts')}")
            print(f"      aliases named:   {p.get('aliases_named')}")
        print()
        sys.stdout.flush()

        out["targets"].append(rec)

    # --- verdicts ----------------------------------------------------------
    # A target is only explained when a pin produced rows where bare did not.
    # Everything else stays "unexplained" on purpose: the whole failure mode
    # this repo keeps re-learning is treating an absent answer as a known one.
    print("=" * 72)
    print("VERDICTS")
    print("=" * 72)
    for rec in out["targets"]:
        rec["verdict"] = verdict_for(rec)
        print(f"  {rec['slug']:<32} {rec['role']:<8} {rec['verdict']}")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
