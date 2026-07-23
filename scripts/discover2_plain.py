"""Discovery probe #2 (plain HTTP): ground-truth the chronogolf + foreup
courses from GitHub's runner using the real adapters, and sniff the two
long-tail engines (SuperSaaS, TgsWeb) for scrapability.
"""
from __future__ import annotations

import datetime as dt
import sys

import requests

sys.path.insert(0, ".")
from scraper.aggregate import load_registry          # noqa: E402
from scraper.adapters.chronogolf import ChronogolfAdapter  # noqa: E402
from scraper.adapters.foreup import ForeUpAdapter    # noqa: E402
from scraper.adapters.teeitup import TeeItUpAdapter  # noqa: E402
from scraper.adapters.base import USER_AGENT         # noqa: E402


def main():
    date = dt.date.today() + dt.timedelta(days=1)
    reg = load_registry("registry.json")

    print("===== CHRONOGOLF (all) =====", flush=True)
    ad = ChronogolfAdapter()
    for c in [x for x in reg if x["platform"] == "chronogolf"]:
        try:
            tts = ad.fetch(c, date)
            print(f"RESULT chrono {c['slug']}: OK {len(tts)} times", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT chrono {c['slug']}: FAIL {type(e).__name__} "
                  f"{str(e)[:110]}", flush=True)

    print("===== FOREUP (needs_ids only) =====", flush=True)
    fu = ForeUpAdapter()
    for c in [x for x in reg if x["platform"] == "foreup"
              and not x["ids"].get("schedule_id")]:
        try:
            found = fu.discover_ids(c["ids"]["course_id"])
            tts = fu.fetch(c, date)
            print(f"RESULT foreup {c['slug']}: OK {len(tts)} times "
                  f"discovered={found}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT foreup {c['slug']}: FAIL {type(e).__name__} "
                  f"{str(e)[:110]}", flush=True)

    print("===== TEEITUP granby =====", flush=True)
    tu = TeeItUpAdapter()
    for c in [x for x in reg if x["slug"] == "golf-granby-ranch"]:
        try:
            tts = tu.fetch(c, date)
            print(f"RESULT teeitup {c['slug']}: OK {len(tts)} times", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT teeitup {c['slug']}: FAIL {type(e).__name__} "
                  f"{str(e)[:150]}", flush=True)

    print("===== LONG TAIL sniff =====", flush=True)
    for name, url in [
        ("supersaas spreading-antlers",
         "https://www.supersaas.com/schedule/Terry's_Golf/SAGC_TEE_TIMES"),
        ("tgsweb walking-stick", "http://173.164.40.54/TgsWeb/Webdll.dll"),
    ]:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
            body = r.text.replace("\n", " ")[:400]
            print(f"RESULT {name}: {r.status_code} len={len(r.text)} "
                  f"head={body!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"RESULT {name}: FAIL {type(e).__name__} {str(e)[:100]}",
                  flush=True)


if __name__ == "__main__":
    main()
