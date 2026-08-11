"""Find each course's public phone number from its own website.

WHY
---
The directory promises a golfer three things about a course we cannot book for
them: how it takes bookings, where its website is, and what number to call.
The first two come from the state CSVs. The third does not — across both
states the CSVs carry exactly five phone numbers, so "call the pro shop" would
have been advice with no number attached.

This fills that in from the one source that is unambiguously allowed and
unambiguously authoritative: the course's own public website, the same page a
golfer would land on from a search result.

Only courses we do NOT serve tee times for are looked up. A course a golfer
can book through OneTee never renders a directory card, so its phone number
would be a number nobody sees, bought with a request to somebody else's web
server. That takes the crawl from 459 sites to 189.

RULES THIS OBEYS
----------------
Public pages only. No logins, no forms, no cookies carried between hosts.
If a host answers with an interactive challenge, that course is SKIPPED and
the reason recorded — a challenge is a "no", and the correct response to a no
is to not have the number, not to defeat the challenge. One request at a time
per host, a real delay between hosts, a short timeout, and a User-Agent that
says who we are. Only ever GET.

HOW IT DECIDES A NUMBER IS RIGHT
--------------------------------
A course website is full of numbers that are not the pro shop: the web
designer's footer, a booking vendor's support line, an 800 number for the
management company. Confidence is scored rather than assumed:

  tel: link                 +3   an explicit machine-readable phone
  near a contact word       +2   "pro shop", "call", "tee times", "phone"
  area code matches state   +2   the strongest single signal for a golf course
  toll-free (8xx)           -2   usually a vendor, rarely a pro shop
  in a script/style block   reject outright

Anything scoring below the floor is left blank. A blank phone renders as no
phone; a wrong phone sends a golfer to a stranger, so the bar is deliberately
set where false negatives are cheap and false positives are not.

  python3 scripts/enrich_phones.py                 # all missing
  python3 scripts/enrich_phones.py --states AZ --limit 20
  python3 scripts/enrich_phones.py --refresh       # re-check ones we have
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter

import requests

DIRECTORY = "directory.json"
OUT = "local/phones.json"
API = "https://api.oneteeapp.com"
STATUS = "probe-results/state-status.json"

UA = ("Mozilla/5.0 (compatible; OneTeeDirectoryBot/1.0; "
      "+https://oneteeapp.com/about) contact: hello@oneteeapp.com")

# Pages a course puts its number on, in the order it is usually on them.
PATHS = ["", "/contact", "/contact-us", "/about", "/about-us", "/course-info"]

PHONE_RE = re.compile(r"\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})")
TEL_RE = re.compile(r'href=["\']tel:([+0-9().\s\-]{7,})["\']', re.I)
# Blocks whose "phone numbers" are coordinates, ids and timestamps.
NOISE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")

CONTACT_WORDS = ("pro shop", "proshop", "call", "tee time", "tee-time",
                 "phone", "reservation", "contact", "tel:")

# Area codes in service for each state. A golf course's number almost always
# matches the state it sits in, which makes this the cheapest strong check we
# have against picking up a vendor's number from the footer.
AREA_CODES = {
    "CO": {"303", "719", "720", "970", "983"},
    "AZ": {"480", "520", "602", "623", "928", "820"},
    "VA": {"276", "434", "540", "571", "703", "757", "804", "826", "948"},
    "FL": {"239", "305", "321", "324", "352", "386", "407", "448", "561",
           "645", "656", "689", "727", "754", "772", "786", "813", "850",
           "863", "904", "941", "954"},
    "MD": {"227", "240", "301", "410", "443", "667"},
}
TOLL_FREE = {"800", "888", "877", "866", "855", "844", "833"}

SCORE_FLOOR = 3


def area_score(a: str, state: str) -> int:
    """Reward an in-state area code; PENALISE an out-of-state one.

    In-state used to be the only signal, worth +2, and out-of-state simply
    scored nothing — which still cleared the floor for a tel: link. So Black
    Bear in Parker, Colorado shipped "(972) 243-6191": a Dallas number, lifted
    from invitedclubs.com, the corporate site its CSV row lists as its website.
    A golfer calling that reaches a management company in Texas, which is worse
    than showing no number at all.

    A golf course is a physical place and uses a local line. An out-of-state
    area code on a course's own site means a corporate switchboard, a booking
    vendor, or an architect's credit — never the pro shop. Toll-free is judged
    separately below; it is merely weak, not wrong.
    """
    known = AREA_CODES.get(state)
    if not known or a in TOLL_FREE:
        return 0
    return 2 if a in known else -2

# Numbers that are structurally impossible for a US phone: NANP forbids 0/1
# as the first digit of an area code or an exchange.
def plausible(a: str, b: str) -> bool:
    return a[0] not in "01" and b[0] not in "01" and a != "555"


def fmt(a: str, b: str, c: str) -> str:
    return f"({a}) {b}-{c}"


def looks_like_challenge(resp: requests.Response, body: str) -> bool:
    """An interactive bot check. We treat this as a refusal and move on."""
    if resp.status_code in (403, 429, 503):
        low = body[:4000].lower()
        if any(m in low for m in ("just a moment", "cf-browser-verification",
                                  "captcha", "verify you are human",
                                  "attention required", "checking your browser")):
            return True
        return resp.status_code in (403, 429)
    return False


def candidates(html: str, state: str) -> list[tuple[int, str]]:
    """Every phone-shaped string on the page with a confidence score."""
    body = NOISE_RE.sub(" ", html)
    found: dict[str, int] = {}

    def add(num: str, score: int) -> None:
        found[num] = max(found.get(num, -99), score)

    for raw in TEL_RE.findall(body):
        digits = re.sub(r"\D", "", raw)
        digits = digits[1:] if len(digits) == 11 and digits[0] == "1" else digits
        if len(digits) != 10:
            continue
        a, b, c = digits[:3], digits[3:6], digits[6:]
        if not plausible(a, b):
            continue
        s = 3 + area_score(a, state)
        s -= 2 if a in TOLL_FREE else 0
        add(fmt(a, b, c), s)

    text = TAG_RE.sub(" ", body)
    text = re.sub(r"\s+", " ", text)
    for m in PHONE_RE.finditer(text):
        a, b, c = m.group(1), m.group(2), m.group(3)
        if not plausible(a, b):
            continue
        window = text[max(0, m.start() - 120):m.end() + 60].lower()
        s = 0
        s += 2 if any(w in window for w in CONTACT_WORDS) else 0
        s += area_score(a, state)
        s -= 2 if a in TOLL_FREE else 0
        # A fax sits right next to the pro shop number and scores identically
        # on every other signal. Printing it as "call to book" is worse than
        # printing nothing, so it has to lose outright.
        # Only the text BEFORE the number: "Fax: 555-1234" labels the number
        # that follows it, and a trailing window would tar the pro shop line
        # sitting immediately above the fax line.
        s -= 6 if "fax" in text[max(0, m.start() - 25):m.start()].lower() else 0
        add(fmt(a, b, c), s)

    return sorted(((v, k) for k, v in found.items()), reverse=True)


def fetch(session: requests.Session, url: str, timeout: int) -> tuple[str, str]:
    """Returns (html, reason). Exactly one of them is non-empty."""
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return "", f"{type(e).__name__}"
    body = r.text or ""
    if looks_like_challenge(r, body):
        return "", "challenge"
    if r.status_code >= 400:
        return "", f"http{r.status_code}"
    if "text/html" not in r.headers.get("content-type", "text/html"):
        return "", "nothtml"
    return body, ""


def phone_for(session: requests.Session, site: str, state: str,
              timeout: int, delay: float) -> tuple[str, str]:
    """(phone, reason). Walks a few likely pages, stops as soon as it is sure."""
    base = site.rstrip("/")
    best: tuple[int, str] = (-99, "")
    reason = "nopage"
    for i, path in enumerate(PATHS):
        if i:
            time.sleep(delay)
        html, why = fetch(session, base + path, timeout)
        if not html:
            # A challenge on the homepage means the whole host is closed to us.
            if why == "challenge":
                return "", "challenge"
            reason = why if i == 0 else reason
            continue
        reason = "nophone"
        for score, num in candidates(html, state):
            if score > best[0]:
                best = (score, num)
        # A tel: link whose area code matches the state is as good as it gets;
        # no later page is going to beat it, so stop paying for requests.
        if best[0] >= 5:
            break
    if best[0] >= SCORE_FLOOR:
        return best[1], "ok"
    return "", ("lowconf" if best[1] else reason)


def live_venues(api: str, status_path: str) -> tuple[set, str]:
    """Venues we are already serving tee times for. (ids, where they came from)

    These are skipped: a course a golfer can book through OneTee never shows a
    directory card, so a phone number for it would be a number nobody sees,
    bought with a request to somebody else's web server. Skipping them roughly
    halves the crawl.

    Live truth comes from the API. If that is unreachable, the last committed
    state-status report is used instead — stale by up to a day, which at worst
    means re-checking a course that just went live. Both failing means we
    crawl everything, which is wasteful but never wrong.
    """
    try:
        r = requests.get(api.rstrip("/") + "/api/courses", timeout=30)
        ids = {c.get("venue_id") or c.get("course_slug")
               for c in r.json().get("courses", [])}
        ids.discard(None)
        if ids:
            return ids, "the live API"
    except (requests.RequestException, ValueError):
        pass
    try:
        with open(status_path) as fh:
            doc = json.load(fh)
        ids = {it["slug"] for st in doc.get("states", [])
               for it in st.get("detail", {}).get("live", [])}
        if ids:
            return ids, f"{status_path} (API unreachable)"
    except (OSError, ValueError, KeyError):
        pass
    return set(), "nowhere — checking every course"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", help="comma-separated, e.g. AZ,CO")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-check courses that already have a number")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between requests")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--api", default=API)
    ap.add_argument("--include-live", action="store_true",
                    help="also look up courses we already serve tee times for "
                         "(their number is never rendered — see live_venues)")
    a = ap.parse_args()

    with open(DIRECTORY) as fh:
        courses = json.load(fh)["courses"]
    states = {s.strip().upper() for s in a.states.split(",")} if a.states else None

    known: dict[str, str] = {}
    if os.path.exists(a.out):
        with open(a.out) as fh:
            known = json.load(fh)

    live, live_src = (set(), "skipped") if a.include_live else \
        live_venues(a.api, STATUS)

    todo = [c for c in courses
            if c["website"]
            and (not states or c["state"] in states)
            and c["venue_id"] not in live
            and (a.refresh or not (known.get(c["venue_id"]) or c["phone"]))]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} course sites to check (of {len(courses)}; "
          f"{len(live)} already serving tee times, from {live_src}; "
          f"{len(known)} numbers already known)", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": UA,
                            "Accept": "text/html,application/xhtml+xml"})

    reasons: Counter = Counter()
    # Group by host so a course with several sub-courses does not get hit
    # back-to-back from a different part of the list.
    todo.sort(key=lambda c: urllib.parse.urlparse(c["website"]).netloc)
    for i, c in enumerate(todo, 1):
        phone, why = phone_for(session, c["website"], c["state"],
                               a.timeout, a.delay)
        reasons[why] += 1
        if phone:
            known[c["venue_id"]] = phone
        print(f"  [{i}/{len(todo)}] {c['state']} {c['name'][:40]:<40} "
              f"{phone or '—':<16} {why}", flush=True)
        if i % 25 == 0:                       # checkpoint: a timeout mid-run
            _save(a.out, known)               # must not throw away the work
        time.sleep(a.delay)

    _save(a.out, known)
    print(f"\nwrote {a.out}: {len(known)} numbers")
    print("outcomes:", dict(reasons))
    return 0


def _save(path: str, known: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(dict(sorted(known.items())), fh, indent=1)


if __name__ == "__main__":
    sys.exit(main())
