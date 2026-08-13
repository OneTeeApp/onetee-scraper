"""Compare sequential vs lane-parallel browser passes from a probe run.

Companion to .github/workflows/probe-browser-lanes.yml. Reads what that job
left in probe-logs/ and probe-out/ and prints one table.

WALL TIME IS THE HEADLINE, COVERAGE IS THE VERDICT. A parallel pass that is
twice as fast and quietly scraped two-thirds of the courses is a regression
dressed as a win, and it would be invisible in production because every
invocation there ends in `|| true`. So this reports, per platform and per mode,
the number of DISTINCT course_slugs that came back and the number that errored.

Slot counts are printed too but deliberately NOT used as the pass/fail signal:
inventory books and releases between passes, so slot totals legitimately move
by a few percent minute to minute. The SET OF COURSES THAT ANSWERED does not.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import defaultdict

LOGS = pathlib.Path("probe-logs")
OUTS = pathlib.Path("probe-out")

# out-file prefix -> platform. clubcaddie and cps write their own names.
PREFIX = {"ez": "ezlinks", "gn": "golfnow", "ss": "supersaas",
          "gwa": "golfwithaccess", "cc": "clubcaddie", "cps": "cps"}


def platform_of(path: pathlib.Path) -> str:
    stem = path.stem
    for pfx in sorted(PREFIX, key=len, reverse=True):
        if stem.startswith(pfx + "_"):
            return PREFIX[pfx]
    return "?"


def read_mode(tag: str) -> dict:
    """Coverage per platform for one mode, from the docs it wrote."""
    per = defaultdict(lambda: {"courses": set(), "slots": 0, "errors": set()})
    d = OUTS / tag
    if not d.is_dir():
        return {}
    for f in sorted(d.glob("*.json")):
        plat = platform_of(f)
        try:
            doc = json.loads(f.read_text())
        except Exception as e:                      # noqa: BLE001
            print(f"  ! unreadable {f.name}: {e}")
            continue
        for t in doc.get("tee_times") or []:
            per[plat]["courses"].add(t.get("course_slug"))
            per[plat]["slots"] += 1
        for e in doc.get("errors") or []:
            per[plat]["errors"].add(e.get("course"))
    return per


def timings(tag: str) -> tuple[dict, int | None, dict]:
    """Per-platform seconds, mode wall time, and lane exit codes."""
    f = LOGS / f"{tag}.log"
    if not f.is_file():
        return {}, None, {}
    txt = f.read_text(errors="replace")
    per = {m.group(1): int(m.group(2))
           for m in re.finditer(r"^TIMING (\S+) (\d+)$", txt, re.M)}
    wall = re.search(r"^MODE \S+ WALL (\d+)$", txt, re.M)
    rcs = {m.group(1): int(m.group(2))
           for m in re.finditer(r"^LANE_RC (\d+) (\d+)$", txt, re.M)}
    return per, (int(wall.group(1)) if wall else None), rcs


def peak_mem(tag: str) -> int | None:
    f = LOGS / f"mem_{tag}.log"
    if not f.is_file():
        return None
    vals = [int(x) for x in f.read_text().split() if x.isdigit()]
    return max(vals) if vals else None


def main() -> int:
    tags = sorted(p.name for p in OUTS.iterdir() if p.is_dir()) if OUTS.is_dir() else []
    if not tags:
        print("no probe output found — the run produced nothing to compare")
        return 0

    modes = {t: (read_mode(t), *timings(t), peak_mem(t)) for t in tags}

    print("=" * 78)
    print("WALL TIME AND MEMORY")
    print("=" * 78)
    print(f"{'mode':>10} {'wall':>8} {'peak MB':>9}  lane exit codes")
    for t in tags:
        _, _, wall, rcs, mem = modes[t]
        rc = " ".join(f"lane{k}={v}" for k, v in sorted(rcs.items())) or "-"
        bad = any(v != 0 for v in rcs.values())
        print(f"{t:>10} {str(wall) + 's':>8} {str(mem):>9}  {rc}"
              + ("   <-- A LANE FAILED" if bad else ""))

    seqs = [t for t in tags if t.startswith("seq")]
    pars = [t for t in tags if t.startswith("par")]
    if seqs and pars:
        sw = [modes[t][2] for t in seqs if modes[t][2]]
        pw = [modes[t][2] for t in pars if modes[t][2]]
        if sw and pw:
            base = sum(sw) / len(sw)
            par = sum(pw) / len(pw)
            print(f"\n  sequential mean {base:.0f}s | parallel mean {par:.0f}s"
                  f" | speedup {base / par:.2f}x")
            if len(sw) > 1:
                drift = abs(sw[0] - sw[-1])
                print(f"  drift between the two sequential passes: {drift}s "
                      f"({drift / base * 100:.0f}% of baseline) — any parallel"
                      f" delta smaller than this is noise")

    print("\n" + "=" * 78)
    print("PER-PLATFORM SECONDS")
    print("=" * 78)
    plats = sorted({p for t in tags for p in modes[t][1]})
    print(f"{'platform':>16} " + " ".join(f"{t:>9}" for t in tags))
    for p in plats:
        print(f"{p:>16} " + " ".join(f"{modes[t][1].get(p, '-'):>9}" for t in tags))

    print("\n" + "=" * 78)
    print("COVERAGE — THE VERDICT (distinct courses that answered / errored)")
    print("=" * 78)
    print(f"{'platform':>16} " + " ".join(f"{t:>16}" for t in tags))
    verdict_ok = True
    allp = sorted({p for t in tags for p in modes[t][0]})
    for p in allp:
        cells = []
        counts = []
        for t in tags:
            e = modes[t][0].get(p)
            if not e:
                cells.append(f"{'-':>16}")
                counts.append(0)
                continue
            n = len(e["courses"])
            counts.append(n)
            cells.append(f"{n:>5}c {e['slots']:>5}s {len(e['errors']):>2}e")
        print(f"{p:>16} " + " ".join(cells))
        base = max(counts) if counts else 0
        # A mode that returned <90% of the best mode's course count for a
        # platform is the silent-failure signature this probe exists to catch.
        if base and min(counts) < base * 0.9:
            verdict_ok = False

    print("\n  legend: <courses>c <slots>s <errors>e")
    if verdict_ok:
        print("\n  COVERAGE OK — every mode returned within 10% of the best "
              "course count on every platform.")
    else:
        print("\n  *** COVERAGE MISMATCH *** at least one platform returned "
              "materially fewer courses in one mode.\n"
              "  Do NOT enable lanes in production on this result — that is the"
              " silent half-failure this probe was built to find.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
