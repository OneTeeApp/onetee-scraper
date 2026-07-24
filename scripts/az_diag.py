"""Diagnose AZ plain-platform fetch failures: run the real adapters for each
AZ teeitup/chronogolf/quick18/teesnap/foreup course (tomorrow) and report
OK/count or the exact error."""
from __future__ import annotations
import datetime as dt, sys
from collections import Counter
sys.path.insert(0, ".")
from scraper.aggregate import load_registry, ADAPTERS

DATE = dt.date.today() + dt.timedelta(days=1)

def main():
    reg = load_registry("registry.json")
    az = [c for c in reg if c.get("state") == "AZ"
          and c["platform"] in ("teeitup","chronogolf","quick18","teesnap","foreup")]
    outcome = Counter()
    errs = Counter()
    for c in sorted(az, key=lambda x: (x["platform"], x["slug"])):
        cls = ADAPTERS.get(c["platform"])
        try:
            tts = cls().fetch(c, DATE)
            if tts:
                outcome[f"{c['platform']}:OK"] += 1
                print(f"OK   {c['platform']:<10} {c['slug']:<38} {len(tts)}", flush=True)
            else:
                outcome[f"{c['platform']}:EMPTY"] += 1
                print(f"EMPTY {c['platform']:<9} {c['slug']:<38} 0", flush=True)
        except Exception as e:
            outcome[f"{c['platform']}:FAIL"] += 1
            key = f"{c['platform']}:{type(e).__name__}"
            errs[key] += 1
            print(f"FAIL {c['platform']:<10} {c['slug']:<38} {type(e).__name__}: {str(e)[:80]}", flush=True)
    print("\n== OUTCOME ==", flush=True)
    for k, n in sorted(outcome.items()): print(f"  {k:<24} {n}", flush=True)
    print("== ERROR TYPES ==", flush=True)
    for k, n in errs.most_common(): print(f"  {k:<28} {n}", flush=True)

if __name__ == "__main__": main()
