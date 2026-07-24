"""Test marketplace/v2/teetimes directly (course UUIDs) for the empty clubs."""
from __future__ import annotations
import datetime as dt, json, sys
sys.path.insert(0, ".")
from scraper.adapters.chronogolf import ChronogolfAdapter, BASE

D1 = (dt.date.today() + dt.timedelta(days=1)).isoformat()
D7 = (dt.date.today() + dt.timedelta(days=7)).isoformat()
CLUBS = ["laughlin-ranch-golf-course",  # control
         "troon-north-golf-club", "the-foothills-golf-club",
         "dove-valley-ranch-golf-club", "talking-stick-golf-club",
         "westin-kierland-golf-club", "alpine-country-club-arizona"]

def main():
    ad = ChronogolfAdapter()
    for slug in CLUBS:
        print(f"\n===== {slug} =====", flush=True)
        try:
            club = ad._club(slug); cid = club["id"]
            courses = ad._courses(cid)
        except Exception as e:
            print(f"  discover FAIL {type(e).__name__}", flush=True); continue
        uuids = [(c.get("id"), c.get("name"), c.get("uuid")) for c in courses]
        print(f"  courses: {[(i,n,('uuid' if u else 'NO-UUID')) for i,n,u in uuids]}", flush=True)
        for i, n, u in uuids:
            if not u: continue
            for d in (D1, D7):
                try:
                    r = ad.get_json(f"{BASE}/marketplace/v2/teetimes",
                        params={"start_date": d, "course_ids": u,
                                "holes": "9,18", "page": 1})
                    tt = r.get("teetimes") if isinstance(r, dict) else None
                    status = r.get("status") if isinstance(r, dict) else None
                    print(f"  v2 {n[:22]:<22} {d}: status={status} "
                          f"teetimes={len(tt) if isinstance(tt,list) else tt}", flush=True)
                    if isinstance(tt, list) and tt:
                        s0 = tt[0]
                        print(f"    first: {json.dumps({k: s0.get(k) for k in ('start_time','date','out_of_capacity','green_fees') if k in s0})[:200]}", flush=True)
                        gf = s0.get("green_fees") or s0.get("rates")
                        print(f"    slot keys: {sorted(s0.keys())[:18]}", flush=True)
                        break
                except Exception as e:
                    print(f"  v2 {n[:22]:<22} {d}: FAIL {type(e).__name__} {str(e)[:60]}", flush=True)

if __name__ == "__main__":
    main()
