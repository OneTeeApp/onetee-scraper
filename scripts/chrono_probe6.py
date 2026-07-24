"""Classify ALL AZ Chronogolf clubs: claimed/bookable vs unclaimed directory
stub. Cheap plain-HTTP signals — club-level online_booking_enabled, has_online_
deals, active, seller presence, and the v1 teetimes count for tomorrow."""
from __future__ import annotations
import datetime as dt, json, sys
from collections import Counter
sys.path.insert(0, ".")
from scraper.aggregate import load_registry
from scraper.adapters.chronogolf import ChronogolfAdapter, BASE

DATE = (dt.date.today() + dt.timedelta(days=1)).isoformat()

def main():
    ad = ChronogolfAdapter()
    reg = load_registry("registry.json")
    az = [c for c in reg if c.get("state") == "AZ" and c["platform"] == "chronogolf"]
    cat = Counter()
    claimed, unclaimed = [], []
    for c in sorted(az, key=lambda x: x["slug"]):
        key = c["ids"].get("club_id") or c["ids"].get("slug")
        try:
            club = ad._club(str(key))
        except Exception as e:
            print(f"  {c['slug']:<40} CLUB-FAIL {type(e).__name__}", flush=True)
            cat["club-fail"] += 1; continue
        cid = club.get("id")
        ob = club.get("online_booking_enabled")
        deals = club.get("has_online_deals")
        seller = bool(club.get("seller"))
        aff = (club.get("settings") or {}).get("default_affiliation_type_id")
        # count v1 teetimes with default aff on the first online course
        courses = [x for x in ad._courses(cid) if x.get("online_booking_enabled")]
        n = None
        if courses and aff:
            try:
                slots = ad.get_json(f"{BASE}/marketplace/clubs/{cid}/teetimes",
                    params=[("date",DATE),("course_id",courses[0]["id"]),
                            ("affiliation_type_ids[]",aff),("affiliation_type_ids[]",aff),
                            ("nb_holes",18)])
                n = len(slots) if isinstance(slots, list) else "?"
            except Exception as e:
                n = f"ERR:{type(e).__name__}"
        flag = "CLAIMED" if ob else "UNCLAIMED"
        (claimed if ob else unclaimed).append(c["slug"])
        cat[flag] += 1
        print(f"  {c['slug']:<40} ob={ob} deals={deals} seller={seller} "
              f"oncourses={len(courses)} v1times={n}", flush=True)
    print(f"\n== SUMMARY: {dict(cat)} ==", flush=True)
    print(f"CLAIMED ({len(claimed)}): {claimed}", flush=True)
    print(f"UNCLAIMED ({len(unclaimed)}): {unclaimed}", flush=True)

if __name__ == "__main__":
    main()
