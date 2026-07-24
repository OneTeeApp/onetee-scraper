"""Verify the SuperSaaS adapter end-to-end for Spreading Antlers, next 3 days."""
import datetime as dt, sys
sys.path.insert(0, ".")
from scraper.aggregate import load_registry, ADAPTERS
reg = load_registry("registry.json")
c = [x for x in reg if x["slug"] == "spreading-antlers-golf-course"][0]
print("course:", c["platform"], c["ids"], flush=True)
ad = ADAPTERS["supersaas"]()
for n in range(3):
    d = dt.date.today() + dt.timedelta(days=n)
    try:
        tts = ad.fetch(c, d)
        eg = tts[0].to_dict() if tts else None
        print(f"  {d}: {len(tts)} open slots  e.g. {eg and (eg['teetime'], eg['state'])}", flush=True)
    except Exception as e:
        print(f"  {d}: FAIL {type(e).__name__}: {str(e)[:80]}", flush=True)
