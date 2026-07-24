"""Why do 37 AZ Chronogolf clubs return zero tee times?

For a sample of EMPTY clubs plus WORKING controls, dump the discovery chain
(club settings, affiliation types, courses w/ holes + online flags) and try
marketplace teetimes with parameter variations (nb_holes 18/9/none, single
aff, alternate affs, +free_slots). Plain HTTP — chronogolf works from GH."""
from __future__ import annotations
import datetime as dt, json, sys
sys.path.insert(0, ".")
from scraper.adapters.chronogolf import ChronogolfAdapter, BASE

DATE = (dt.date.today() + dt.timedelta(days=1)).isoformat()
EMPTY = ["troon-north-golf-club", "talking-stick-golf-club",
         "the-foothills-golf-club", "dove-valley-ranch-golf-club",
         "alpine-country-club-arizona", "villa-monterey-public-golf-club",
         "westin-kierland-golf-club", "lake-powell-national-golf-club"]
WORKING = ["laughlin-ranch-golf-course", "payson-golf-course"]

def probe(ad, slug, label):
    print(f"\n===== {label}: {slug} =====", flush=True)
    try:
        club = ad._club(slug)
    except Exception as e:
        print(f"  club FAIL {type(e).__name__}: {str(e)[:100]}", flush=True)
        return
    cid = club.get("id")
    settings = club.get("settings") or {}
    aff_default = settings.get("default_affiliation_type_id")
    # surface any affiliation-type info on the club payload
    affs = club.get("affiliation_types") or settings.get("affiliation_types")
    keys = sorted(club.keys())
    print(f"  club_id={cid} default_aff={aff_default}", flush=True)
    print(f"  club keys: {keys}", flush=True)
    interesting = {k: club.get(k) for k in
                   ("marketplace_enabled", "online_booking", "booking_url",
                    "reservation_enabled", "status", "uuid") if k in club}
    print(f"  club flags: {json.dumps(interesting)[:300]}", flush=True)
    print(f"  settings: {json.dumps(settings)[:400]}", flush=True)
    if affs: print(f"  affiliation_types: {json.dumps(affs)[:400]}", flush=True)
    try:
        courses = ad._courses(cid)
    except Exception as e:
        print(f"  courses FAIL {type(e).__name__}", flush=True)
        return
    for c in courses:
        print(f"  course id={c.get('id')} name={c.get('name')!r} "
              f"holes={c.get('nb_holes') or c.get('holes')} "
              f"online={c.get('online_booking_enabled')} "
              f"keys={sorted(set(c.keys()) - {'id','name'})[:12]}", flush=True)
    on = [c for c in courses if c.get("online_booking_enabled")]
    targets = on or courses
    if not targets:
        print("  NO COURSES AT ALL", flush=True)
        return
    c0 = targets[0]["id"]
    variations = [
        ("18-2aff", [("date", DATE), ("course_id", c0),
                     ("affiliation_type_ids[]", aff_default),
                     ("affiliation_type_ids[]", aff_default), ("nb_holes", 18)]),
        ("9-2aff",  [("date", DATE), ("course_id", c0),
                     ("affiliation_type_ids[]", aff_default),
                     ("affiliation_type_ids[]", aff_default), ("nb_holes", 9)]),
        ("noholes", [("date", DATE), ("course_id", c0),
                     ("affiliation_type_ids[]", aff_default),
                     ("affiliation_type_ids[]", aff_default)]),
        ("1aff-18", [("date", DATE), ("course_id", c0),
                     ("affiliation_type_ids[]", aff_default), ("nb_holes", 18)]),
        ("free",    [("date", DATE), ("course_id", c0),
                     ("affiliation_type_ids[]", aff_default),
                     ("affiliation_type_ids[]", aff_default), ("nb_holes", 18),
                     ("free_slots", 1)]),
    ]
    for name, params in variations:
        try:
            slots = ad.get_json(f"{BASE}/marketplace/clubs/{cid}/teetimes",
                                params=params)
            n = len(slots) if isinstance(slots, list) else f"non-list:{type(slots).__name__}"
            extra = ""
            if isinstance(slots, list) and slots:
                s0 = slots[0]
                extra = f" first={json.dumps({k: s0.get(k) for k in ('start_time','date','out_of_capacity','hole')})}"
            print(f"  [{name}] -> {n}{extra}", flush=True)
        except Exception as e:
            print(f"  [{name}] -> {type(e).__name__}: {str(e)[:90]}", flush=True)

def main():
    ad = ChronogolfAdapter()
    for s in WORKING: probe(ad, s, "WORKING")
    for s in EMPTY: probe(ad, s, "EMPTY")

if __name__ == "__main__":
    main()
