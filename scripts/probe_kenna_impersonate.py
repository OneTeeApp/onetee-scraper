"""Probe: does curl_cffi (Chrome TLS impersonation) clear kenna's throttle from
a data-center runner, where plain python-requests gets throttled? If yes, the
whole teeitup browser tier can be replaced by fast HTTP and moved to the 5-min
cadence. Compares plain requests vs curl_cffi for a few CO courses at a FAR date
(where the throttle bites hardest). Prints a table; no writes."""
import datetime as dt
import requests
try:
    from curl_cffi import requests as creq
except Exception as e:  # noqa: BLE001
    creq = None
    print("curl_cffi import failed:", e)

KENNA = "https://phx-api-be-east-1b.kenna.io/v2/tee-times"
# (alias, facilityId) — different aliases, all CO
TESTS = [
    ("commonground-golf-course", "5275"),
    ("raccoon-creek-golf-course", "515"),
    ("riverdale", "1017"),
]
FAR = (dt.date.today() + dt.timedelta(days=16)).isoformat()
NEAR = (dt.date.today() + dt.timedelta(days=2)).isoformat()


def count(getter, alias, fid, date):
    hdr = {"x-be-alias": alias, "accept": "application/json",
           "Origin": f"https://{alias}.book.teeitup.com",
           "Referer": f"https://{alias}.book.teeitup.com/"}
    params = {"date": date, "facilityIds": fid, "returnPromotedRates": "true"}
    try:
        r = getter(hdr, params)
        if r.status_code != 200:
            return f"HTTP{r.status_code}"
        j = r.json()
        block = j[0] if isinstance(j, list) and j else {}
        return len((block or {}).get("teetimes", []))
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}"


def plain(hdr, params):
    return requests.get(KENNA, headers=hdr, params=params, timeout=25)


def imp(hdr, params):
    return creq.get(KENNA, headers=hdr, params=params, impersonate="chrome", timeout=25)


print(f"kenna throttle probe — NEAR {NEAR} / FAR {FAR}")
for alias, fid in TESTS:
    for label, date in (("near", NEAR), ("far", FAR)):
        p = count(plain, alias, fid, date)
        i = count(imp, alias, fid, date) if creq else "n/a"
        print(f"  {alias:28s} {label} plain={p!s:8s} curl_cffi={i}")
