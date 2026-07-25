"""Is Teesnap's ?course=<id> scoped to the subdomain, or global?

diag_teesnap.txt turned up something that changes the whole reading of the
"casualties":

  * lakehavasu ?course=1517 returns 60 slots — but 1517 is the id of an
    entry in courses[0].infos, a text notice, not a course.
  * sundancegolfclub ?course=1785 returns 60 slots — 1785 is likewise an
    infos id.
  * heathergardens ?course=131 returns 78 slots while its only real course
    (148) returns 0. 131 is that course's property_id.

Info-row ids and property ids live in different tables from courses, so they
cannot be courses on that tenant. The obvious explanation is that
customer-api/teetimes-day resolves `course` GLOBALLY and ignores which
subdomain asked — in which case the id the old regex swept up was some other
club's tee sheet, and every slot it "recovered" was foreign data published
under our course's name.

That would invert the finding: the two "casualties" would be correctness
wins, and the old before= numbers partly other people's inventory.

This tests it directly. Same id, several subdomains, same date. If the
payloads are identical byte-for-byte then the subdomain is decorative and
only ids from THAT tenant's own window.courses may ever be used.

Public endpoints, report only. No credentials, no CAPTCHA, no TLS forgery.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teesnap import TeesnapAdapter  # noqa: E402

DATE = dt.date.today() + dt.timedelta(days=1)

SUBS = ["heathergardens", "lakehavasu", "mtmassivegolf", "sundancegolfclub"]

# id -> where diag_teesnap.txt found it
IDS = {
    148: "heathergardens' only real course (returns 0 there)",
    131: "heathergardens course[0].property_id (returns 78 there)",
    307: "lakehavasu West, a real course (returns 0 there)",
    308: "lakehavasu East, a real course (returns 75 there)",
    1517: "lakehavasu courses[0].infos[0].id — a TEXT NOTICE (returns 60)",
    966: "mtmassivegolf's only real course (returns 76 there)",
    862: "mtmassivegolf course[0].property_id (returns 68 there)",
    1801: "sundancegolfclub's only real course (returns 90 there)",
    1785: "sundancegolfclub courses[0].infos[2].id — TEXT (returns 60)",
    1: "a control id belonging to nobody in this sample",
}


def sheet(ad: TeesnapAdapter, sub: str, cid: int) -> dict:
    url = f"https://{sub}.teesnap.net/customer-api/teetimes-day"
    params = {"course": cid, "date": DATE.isoformat(),
              "players": 1, "holes": 18, "addons": "off"}
    try:
        r = ad.session.get(url, params=params, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return {"err": f"{type(exc).__name__}: {str(exc)[:50]}"}
    if r.status_code != 200:
        return {"err": f"HTTP {r.status_code}"}
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return {"err": "not JSON"}
    slots = ((data or {}).get("teeTimes", {}) or {}).get("teeTimes", []) or []
    times = []
    for s in slots:
        for sec in s.get("teeOffSections") or []:
            t = (sec.get("turnTo") or {}).get("time") or sec.get("time")
            if t:
                times.append(str(t))
        if not (s.get("teeOffSections") or []) and s.get("teeTime"):
            times.append(str(s["teeTime"]))
    prices = sorted({str(p.get("price")) for s in slots
                     for p in (s.get("prices") or [])})
    fingerprint = hashlib.sha1(
        ("|".join(sorted(times)) + "//" + ",".join(prices)).encode()
    ).hexdigest()[:12]
    return {"n": len(times), "first": times[0] if times else "",
            "last": max(times) if times else "",
            "prices": ",".join(prices[:4]),
            "fp": fingerprint if times else "-empty-"}


def main() -> None:
    print("diag_teesnap2: is ?course=<id> scoped to the subdomain or global?")
    print(f"date probed: {DATE.isoformat()}")
    print("Report only. Nothing here edits the adapter, the registry, or D1.")
    ad = TeesnapAdapter()
    verdicts: list[str] = []
    for cid, where in IDS.items():
        print("\n" + "=" * 72)
        print(f"?course={cid}   [{where}]")
        print("=" * 72)
        fps: dict[str, str] = {}
        for sub in SUBS:
            r = sheet(ad, sub, cid)
            if "err" in r:
                print(f"    {sub:20s} {r['err']}")
                fps[sub] = "ERR:" + r["err"]
                continue
            print(f"    {sub:20s} {r['n']:>4} times  first={r['first']}  "
                  f"last={r['last']}  prices={r['prices']}  fp={r['fp']}")
            fps[sub] = r["fp"]
            sys.stdout.flush()
        distinct = set(fps.values())
        if len(distinct) == 1:
            v = (f"course={cid}: IDENTICAL on all {len(SUBS)} subdomains "
                 f"({distinct.pop()}) -> subdomain ignored")
        else:
            v = (f"course={cid}: DIFFERS by subdomain -> "
                 + "; ".join(f"{s}={f}" for s, f in fps.items()))
        print(f"    => {v}")
        verdicts.append(v)

    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)
    for v in verdicts:
        print("  " + v)
    print("\nIf every id reads the same on every subdomain, the tenant host is "
          "decorative: an id that is not in THAT tenant's own window.courses "
          "is some other club's sheet, and must never be scraped under this "
          "course's name.")
    print("\ndone")


if __name__ == "__main__":
    main()
