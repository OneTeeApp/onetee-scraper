"""Verify the native booking engines probe-results/native_az.txt turned up.

native_probe.py only proves a course's WEBSITE links to an engine. Before we
move a row off GolfNow we need the stronger claim: our adapter, given these
ids, returns that course's real tee sheet. A link can be stale, can point at a
sibling club, or can lead to a portal that needs a login.

So this calls the REAL adapters for three dates and reports what came back —
counts, first/last time, price range, and any sub-course labels, which is how
a wrong id gives itself away (another club's name, or a slot count that
matches a neighbour).

Report only. Nothing here edits the CSV or the registry; re-tagging happens by
hand from this output, because a wrong auto-registration publishes another
course's tee sheet under the wrong name.

Public pages only: no credentials, no CAPTCHA solving, no TLS fingerprinting.
"""
from __future__ import annotations

import datetime as dt
import sys
import traceback

sys.path.insert(0, ".")

import requests  # noqa: E402

from scraper.aggregate import ADAPTERS  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Every candidate native engine from probe-results/native_az.txt, with the ids
# read straight out of the URL the course's own website links to.
CANDIDATES = [
    # slug, name, city, platform, ids, source line from native_az.txt
    ("painted-mountain-golf-resort", "Painted Mountain Golf Resort", "Mesa",
     "foreup", {"course_id": "21954", "schedule_id": "9443"},
     "foreupsoftware.com/index.php/booking/21954/9443"),
    ("hillcrest-golf-club", "Hillcrest Golf Club", "Sun City West",
     "foreup", {"course_id": "21953", "schedule_id": "9442"},
     "foreupsoftware.com/index.php/booking/21953/9442"),
    ("continental-country-club", "Continental Country Club", "Flagstaff",
     "foreup", {"course_id": "22092", "schedule_id": "9809"},
     "foreupsoftware.com/index.php/booking/22092/9809"),
    ("chaparral-golf-country-club", "Chaparral Golf & Country Club",
     "Bullhead City", "foreup",
     {"course_id": "20246", "schedule_id": "4086"},
     "foreupsoftware.com/index.php/booking/20246/4086"),
    ("dave-white-municipal-golf-course", "Dave White Municipal Golf Course",
     "Casa Grande", "foreup", {"course_id": "20384", "schedule_id": "4487"},
     "foreupsoftware.com/index.php/booking/20384/4487"),

    ("union-hills-golf-club", "Union Hills Golf Club", "Sun City",
     "quick18", {"subdomain": "unionhills"},
     "unionhills.quick18.com/teetimes/searchmatrix"),
    ("agave-highlands-golf-course", "Agave Highlands Golf Course", "Cornville",
     "quick18", {"subdomain": "agavehighlands"},
     "agavehighlands.quick18.com/teetimes/searchmatrix"),

    ("forty-niner-country-club", "Forty Niner Country Club", "Tucson",
     "teeitup", {"alias": "forty-niner-country-club"},
     "forty-niner-country-club.book.teeitup.com/"),
]

# EZLinks needs its own note: these portals sit behind Cloudflare and the
# plain-HTTP adapter is excluded from normal scrapes in favour of the browser
# fetcher, so a failure here is not evidence the portal is wrong.
EZLINKS = [
    ("arizona-traditions-golf-club", "Arizona Traditions Golf Club", "Surprise",
     "Arizonatraditionseagle", "Arizonatraditionseagle.ezlinks.com"),
    ("forty-niner-country-club", "Forty Niner Country Club", "Tucson",
     "fortyniner", "fortyniner.ezlinksgolf.com/index.html#/search"),
]

# Club Prophet's OTHER portal host. Lake Arbor turned out to be login-gated on
# this host (probe-results/diag3.txt section D) and we have no adapter for it,
# so all we want to know is whether Falcon Dunes is anonymous or gated too.
PROPHETSVC = [
    ("falcon-dunes-golf-course", "Falcon Dunes Golf Course", "Waddell",
     "https://secure.west.prophetservices.com/FalconDunesV3/"),
]

DATES = [dt.date.today() + dt.timedelta(days=d) for d in (1, 3, 7)]


def course_dict(slug, name, city, platform, ids) -> dict:
    return {"slug": slug, "name": name, "city": city, "state": "AZ",
            "platform": platform, "ids": ids, "venue_id": slug,
            "source_role": "primary", "status": "ready",
            "booking_url": "", "lat": None, "lon": None}


def describe(slots) -> str:
    if not slots:
        return "0 slots"
    times = sorted(str(t.teetime) for t in slots)
    prices = [p for t in slots for p in (t.price_min, t.price_max)
              if p is not None]
    labels = sorted({(t.course_label or "").strip() for t in slots} - {""})
    bits = [f"{len(slots)} slots", f"{times[0][11:16]}-{times[-1][11:16]}"]
    if prices:
        bits.append(f"${min(prices):.0f}-${max(prices):.0f}")
    if labels:
        bits.append("labels=" + " | ".join(labels[:4]))
    return "  ".join(bits)


def run_adapter(slug, name, city, platform, ids, source) -> None:
    print(f"\n--- {name}  [{platform}]")
    print(f"    from: {source}")
    print(f"    ids:  {ids}")
    adapter = ADAPTERS[platform]()
    course = course_dict(slug, name, city, platform, ids)
    total = 0
    for d in DATES:
        try:
            slots = adapter.fetch(course, d)
        except Exception as exc:  # noqa: BLE001
            print(f"    {d}: ERROR {type(exc).__name__}: {str(exc)[:160]}")
            continue
        total += len(slots)
        print(f"    {d}: {describe(slots)}")
        if slots:
            s = slots[0]
            print(f"        sample: {s.teetime} holes={s.holes} "
                  f"spots={s.open_spots} label={s.course_label!r}")
    print(f"    VERDICT: {'CONFIRMED' if total else 'NO INVENTORY'} "
          f"({total} slots over {len(DATES)} days)")


def main() -> None:
    print("verify_native_az: calling the real adapters against the engines "
          "native_az.txt found")
    print(f"dates: {', '.join(d.isoformat() for d in DATES)}")
    print("Report only. Nothing here edits the CSV or the registry.\n")

    print("=" * 70)
    print("A. adapters we already run")
    print("=" * 70)
    for row in CANDIDATES:
        try:
            run_adapter(*row)
        except Exception:  # noqa: BLE001
            print("    HARNESS ERROR:")
            traceback.print_exc(limit=3)
        sys.stdout.flush()

    print("\n" + "=" * 70)
    print("B. EZLinks portals (plain HTTP; the fleet uses the browser fetcher,")
    print("   so a Cloudflare block here is not evidence the portal is wrong)")
    print("=" * 70)
    for slug, name, city, portal, source in EZLINKS:
        print(f"\n--- {name}  [ezlinks]")
        print(f"    from: {source}")
        print(f"    portal: {portal}")
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        for host in (f"https://{portal}.ezlinksgolf.com",
                     f"https://{portal}.ezlinks.com"):
            try:
                r = s.get(host + "/api/search/init", timeout=25)
                body = r.text[:200].replace("\n", " ")
                print(f"    {host}: HTTP {r.status_code} {len(r.text)}B  {body[:120]}")
            except Exception as exc:  # noqa: BLE001
                print(f"    {host}: {type(exc).__name__}: {str(exc)[:100]}")
        try:
            run_adapter(slug, name, city, "ezlinks", {"portal": portal}, source)
        except Exception:  # noqa: BLE001
            print("    HARNESS ERROR:")
            traceback.print_exc(limit=3)
        sys.stdout.flush()

    print("\n" + "=" * 70)
    print("C. prophetservices portals — anonymous, or login-gated like Lake Arbor?")
    print("=" * 70)
    for slug, name, city, url in PROPHETSVC:
        print(f"\n--- {name}")
        print(f"    url: {url}")
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        try:
            r = s.get(url, timeout=30, allow_redirects=True)
            final = r.url
            gated = any(k in final.lower() for k in ("logon", "login", "account"))
            print(f"    HTTP {r.status_code} {len(r.text)}B  final={final}")
            print(f"    login-gated by URL? {gated}")
            low = r.text.lower()
            for marker in ("password", "sign in", "log on", "member number",
                           "tee time", "book now"):
                if marker in low:
                    print(f"      page mentions: {marker!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"    {type(exc).__name__}: {str(exc)[:160]}")
        sys.stdout.flush()

    print("\ndone")


if __name__ == "__main__":
    main()
