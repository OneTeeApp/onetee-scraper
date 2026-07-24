"""Find course UUIDs (v2 needs them) and test marketplace/v2/teetimes for the
empty clubs. Try several endpoints to locate course uuids."""
from __future__ import annotations
import datetime as dt, json, sys
sys.path.insert(0, ".")
from scraper.adapters.chronogolf import ChronogolfAdapter, BASE

D1 = (dt.date.today() + dt.timedelta(days=1)).isoformat()
CLUBS = ["laughlin-ranch-golf-course", "troon-north-golf-club",
         "the-foothills-golf-club", "westin-kierland-golf-club"]

def try_json(ad, url, **params):
    try:
        return ad.get_json(url, params=params or None)
    except Exception as e:
        return f"FAIL {type(e).__name__} {str(e)[:50]}"

def main():
    ad = ChronogolfAdapter()
    for slug in CLUBS:
        print(f"\n===== {slug} =====", flush=True)
        club = ad._club(slug); cid = club["id"]; cuuid = club.get("uuid")
        print(f"  club_id={cid} club_uuid={cuuid}", flush=True)
        # candidate endpoints for course uuids
        for name, url in [
            ("v2/clubs/<id>", f"{BASE}/marketplace/v2/clubs/{cid}"),
            ("v2/clubs/<id>/courses", f"{BASE}/marketplace/v2/clubs/{cid}/courses"),
        ]:
            r = try_json(ad, url)
            if isinstance(r, str): print(f"  {name}: {r}", flush=True); continue
            # extract course uuids
            courses = r.get("courses") if isinstance(r, dict) else (r if isinstance(r, list) else [])
            uu = [(c.get("id"), c.get("name"), c.get("uuid")) for c in courses] if courses else None
            if uu:
                print(f"  {name}: courses={uu}", flush=True)
            else:
                print(f"  {name}: keys={sorted(r.keys())[:15] if isinstance(r,dict) else type(r).__name__} len={len(json.dumps(r))}", flush=True)
        # try to pull uuids from wherever we found them, then hit v2 teetimes
        r = try_json(ad, f"{BASE}/marketplace/v2/clubs/{cid}/courses")
        courses = (r.get("courses") if isinstance(r, dict) else r) if not isinstance(r, str) else []
        for c in (courses or []):
            u = c.get("uuid")
            if not u: continue
            tt = try_json(ad, f"{BASE}/marketplace/v2/teetimes",
                          start_date=D1, course_ids=u, holes="9,18", page=1)
            if isinstance(tt, str): print(f"  v2 teetimes {c.get('name')}: {tt}", flush=True); continue
            arr = tt.get("teetimes") if isinstance(tt, dict) else None
            print(f"  v2 teetimes {c.get('name')[:20]:<20} status={tt.get('status') if isinstance(tt,dict) else '?'} "
                  f"n={len(arr) if isinstance(arr,list) else arr}", flush=True)
            if isinstance(arr, list) and arr:
                s0 = arr[0]
                print(f"    slot keys: {sorted(s0.keys())}", flush=True)
                print(f"    sample: {json.dumps({k:s0.get(k) for k in ('start_time','date','starts_at','green_fees','default_product_id','out_of_capacity') if k in s0})[:250]}", flush=True)

if __name__ == "__main__":
    main()
