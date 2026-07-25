"""What do Teesnap tenants ACTUALLY inline in window.courses?

8a5f491 narrowed discover_courses() to the top-level entries of the
`window.courses` array, on the theory that every other `"id":<n>,"created_at"`
in that region is an embedded PROPERTY id rather than a course. #69 measured
that and it is only mostly true (probe-results/verify_fixes.txt section B):

  heather-gardens   old ids [148, 131]                 -> new [148]  78 slots -> 0
  mount-massive     old ids [966, 1933, 1934, 1935, 862] -> new [966]  144 -> 76

So on those two tenants the id I discarded was carrying the real sheet, or
part of it. Rather than guess which of the three filters (deleted_at,
enabled is False, key and name both None) is wrong, this dumps the ground
truth:

  1. every TOP-LEVEL entry of window.courses, with the fields the filters
     read plus the full key list, and where any nested id lives;
  2. every id the OLD regex found, with the JSON path it was found at;
  3. what each id actually returns from customer-api/teetimes-day — HTTP
     status, slot count, and the course name the API echoes back.

(3) is the decider: an id that returns slots is a course no matter what the
homepage JSON calls it.

Public endpoints only. Report only — nothing here edits the adapter, the
CSV, the registry, or D1. No credentials, no CAPTCHA, no TLS forgery.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.adapters.teesnap import TeesnapAdapter  # noqa: E402

DATE = dt.date.today() + dt.timedelta(days=1)

# The two casualties first, then the tenants the change demonstrably fixed,
# so a structural difference between them is visible side by side.
SUBS = [
    ("heathergardens", "CASUALTY 78 -> 0"),
    ("mtmassivegolf", "CASUALTY 144 -> 76"),
    ("lakehavasu", "FIXED, and the only tenant with 2 real top-level courses"),
    ("golfpagosa", "unchanged 44 -> 44"),
    ("hollydotgolf", "FIXED RAISED -> 72"),
    ("petteyspark", "FIXED RAISED -> 59"),
    ("stoneridgegc", "FIXED RAISED -> 68"),
    ("sundancegolfclub", "FIXED RAISED -> 90"),
]

FIELDS = ("id", "key", "name", "enabled", "deleted_at", "holes",
          "min_players", "max_players")


def walk_ids(node, path="courses") -> list[tuple[str, int, str]]:
    """-> [(json path, id, name-ish)] for every dict carrying an int id."""
    found: list[tuple[str, int, str]] = []
    if isinstance(node, list):
        for i, v in enumerate(node):
            found += walk_ids(v, f"{path}[{i}]")
    elif isinstance(node, dict):
        if isinstance(node.get("id"), int):
            label = node.get("name") or node.get("key") or ""
            found.append((path, node["id"], str(label)[:40]))
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                found += walk_ids(v, f"{path}.{k}")
    return found


def probe_id(ad: TeesnapAdapter, sub: str, cid: int) -> str:
    url = f"https://{sub}.teesnap.net/customer-api/teetimes-day"
    params = {"course": cid, "date": DATE.isoformat(),
              "players": 1, "holes": 18, "addons": "off"}
    try:
        r = ad.session.get(url, params=params, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return f"request failed: {type(exc).__name__}: {str(exc)[:60]}"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return f"HTTP 200 but not JSON ({len(r.text)}B)"
    block = (data or {}).get("teeTimes", {}) or {}
    slots = block.get("teeTimes", []) or []
    n = 0
    for s in slots:
        secs = [x for x in (s.get("teeOffSections") or [])
                if (x.get("turnTo") or {}).get("time") or x.get("time")]
        n += len(secs) or (1 if s.get("teeTime") else 0)
    # what does the API itself call this course?
    echo = ""
    for key in ("course", "courseName", "name"):
        v = (data or {}).get(key) or block.get(key)
        if isinstance(v, str) and v:
            echo = v
            break
        if isinstance(v, dict) and v.get("name"):
            echo = str(v["name"])
            break
    first = ""
    if slots:
        s0 = slots[0]
        t = ((s0.get("teeOffSections") or [{}])[0].get("turnTo") or {}).get("time") \
            or s0.get("teeTime")
        first = f" first={t}"
    return (f"HTTP 200  {len(slots)} raw slots / {n} counted"
            + (f"  api-name={echo!r}" if echo else "") + first)


def main() -> None:
    print("diag_teesnap: ground truth for window.courses vs what answers")
    print(f"date probed: {DATE.isoformat()}")
    print("Report only. Nothing here edits the adapter, the registry, or D1.")
    ad = TeesnapAdapter()
    for sub, note in SUBS:
        print("\n" + "=" * 72)
        print(f"{sub}.teesnap.net   [{note}]")
        print("=" * 72)
        try:
            html = ad._get_text(f"https://{sub}.teesnap.net/")
        except Exception as exc:  # noqa: BLE001
            print(f"  homepage fetch failed: {type(exc).__name__}: {str(exc)[:90]}")
            continue

        entries = ad._window_courses_json(html)
        print(f"  window.courses parsed: {len(entries)} top-level entries")
        for i, c in enumerate(entries):
            shown = {k: c.get(k) for k in FIELDS if k in c}
            print(f"    [{i}] {shown}")
            print(f"        all keys: {sorted(c.keys())}")
            # which filter, if any, would drop this entry?
            drops = []
            if not isinstance(c.get("id"), int):
                drops.append("id not int")
            if c.get("deleted_at"):
                drops.append("deleted_at set")
            if c.get("enabled") is False:
                drops.append("enabled False")
            if c.get("key") is None and c.get("name") is None:
                drops.append("key and name both None")
            print(f"        current filter verdict: "
                  f"{'DROPPED (' + ', '.join(drops) + ')' if drops else 'kept'}")

        nested = walk_ids(entries)
        top_ids = {c.get("id") for c in entries if isinstance(c.get("id"), int)}
        print(f"\n  every id anywhere under window.courses ({len(nested)}):")
        for path, cid, label in nested:
            mark = "TOP-LEVEL" if re.fullmatch(r"courses\[\d+\]", path) else "nested"
            print(f"    {cid:>6}  {mark:9s} {path}  {label!r}")

        start = html.find("window.courses")
        region = html[start:start + 30000] if start >= 0 else html
        old_ids: list[int] = []
        for i in re.findall(r'"id":\s*(\d+)\s*,\s*"created_at"', region):
            if int(i) not in old_ids:
                old_ids.append(int(i))
        print(f"\n  old regex ids: {old_ids}")
        print(f"  new top-level ids: {sorted(top_ids)}")
        print(f"  dropped by the change: "
              f"{sorted(set(old_ids) - top_ids)}")

        every = []
        for cid in old_ids + [c for _, c, _ in nested]:
            if cid not in every:
                every.append(cid)
        print(f"\n  what each id returns on {DATE.isoformat()}:")
        for cid in every:
            tag = "top-level" if cid in top_ids else "dropped  "
            print(f"    {cid:>6} [{tag}] {probe_id(ad, sub, cid)}")
        sys.stdout.flush()

        # For the two casualties, dump the raw array so the shape is on record.
        if sub in ("heathergardens", "mtmassivegolf"):
            raw = json.dumps(entries)[:4000]
            print(f"\n  raw window.courses (first 4000 chars):\n{raw}")
            sys.stdout.flush()

    print("\ndone")


if __name__ == "__main__":
    main()
