"""TeeQuest adapter — plain HTML, no auth, no CAPTCHA (captured July 2026).

TeeQuest ships two skins and a course can be on either one. Both are
server-rendered, so both are read with requests + BeautifulSoup:

  LEGACY  https://teetimes.teequest.com/<site>
      POST the search form back to the same URL:
          PaymentTab=pay-online
          Search.CourseTag=<site>-<n>       <- the <select> lists the site's
          Search.Date=M/D/YYYY 12:00:00 AM     own courses, nothing else
          Search.Time=Anytime
          Search.Players=0                  (0 = "Any")
      -> .tee-time
           .time-container   "10:26 am"
           .detail           "$55.00  18 holes with cart  1 2 3 4"
                             one <a> per player count; only the counts that
                             are still open carry text.
      The date <select> is the booking window (10 days when captured).

  V2      https://bookateetime.teequest.com/course/<id>
      GET /search/<id>-1/<YYYY-MM-DD>?selectedPlayers=<n>&selectedHoles=18
      -> .tee-time[data-date-time=YYYYMMDDHHMM]
                  [data-price=79.75][data-booked=0][data-available=4]
      Structured attributes, so no text scraping at all.

WHY selectedPlayers IS TRIED TWICE ON V2

A club can forbid 1-player online booking. Emerald Canyon does, and asking
for 1 returns a red banner plus "0 tee times found" — which is
indistinguishable from a genuinely empty day if you only count rows. So v2
asks for 1 first (widest net) and, when the response carries the
"does not allow single player" notice, retries at 2. Anything that still
returns nothing really is empty.

THE COURSE-TAG RULE (same hazard as teesnap)

A tag is "<site>-<n>". Nothing stops us from posting another site's tag, so
the tag is never guessed: legacy reads the tag out of the page's own
<select name="Search.CourseTag">, v2 out of the page's own
<input name="selectedCourse"> radios. A tag that is not offered by the page
we asked is dropped rather than fetched, because a wrong tag does not error
— it returns somebody else's tee sheet.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter, TIMEOUT
from ..models import TeeTime

LEGACY_HOST = "teetimes.teequest.com"
V2_HOST = "bookateetime.teequest.com"

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)
_PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
_SINGLE_BLOCKED_RE = re.compile(r"does not allow single player", re.I)


def _soup(html: str):
    from bs4 import BeautifulSoup  # lazy: isolate dep to the HTML adapters
    return BeautifulSoup(html, "html.parser")


def _to_iso(date: dt.date, hh: int, mm: int, ampm: str) -> str:
    if ampm.lower() == "p" and hh != 12:
        hh += 12
    elif ampm.lower() == "a" and hh == 12:
        hh = 0
    return f"{date.isoformat()}T{hh:02d}:{mm:02d}:00"


class TeeQuestAdapter(Adapter):
    platform = "teequest"

    # -- entry point ---------------------------------------------------------

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        site = ids.get("site")
        if not site:
            raise ValueError(f"{course['slug']}: no teequest site id")
        skin = ids.get("skin") or ("v2" if ids.get("host") == V2_HOST else "legacy")
        if skin == "v2":
            return self._fetch_v2(course, date, str(site))
        return self._fetch_legacy(course, date, str(site))

    # -- legacy skin ---------------------------------------------------------

    def _legacy_url(self, site: str) -> str:
        return f"https://{LEGACY_HOST}/{site}"

    def _fetch_legacy(self, course: dict, date: dt.date, site: str) -> list[TeeTime]:
        url = self._legacy_url(site)
        shell = self.session.get(url, timeout=TIMEOUT)
        shell.raise_for_status()
        page = _soup(shell.text)

        sel = page.find("select", {"name": "Search.CourseTag"})
        if sel is None:
            raise RuntimeError(f"{course['slug']}: teequest search form not found")
        offered = {o.get("value"): o.get_text(strip=True)
                   for o in sel.find_all("option") if o.get("value")}
        pinned = course["ids"].get("course_tags")
        tags = [t for t in (pinned or offered)] if pinned else list(offered)
        rejected = [t for t in tags if t not in offered]
        if rejected:
            # Never fetch a tag this site does not offer — see the module
            # docstring. Dropping it loses a course; guessing publishes the
            # wrong club's tee sheet under our course's name.
            tags = [t for t in tags if t in offered]
        if not tags:
            raise RuntimeError(f"{course['slug']}: no usable teequest course tag "
                               f"(rejected {rejected})")

        # Label when MORE THAN ONE TAG IS FETCHED — not "when more than one is
        # pinned". An unpinned multi-course site fetches every offered tag, and
        # the old pinned-count test left all of them labelled "", collapsing
        # same-time slots from different courses onto one D1 key.
        multi = len(tags) > 1
        out: list[TeeTime] = []
        for tag in tags:
            body = {
                "PaymentTab": "pay-online",
                "Search.CourseTag": tag,
                "Search.Date": f"{date.month}/{date.day}/{date.year} 12:00:00 AM",
                "Search.Time": "Anytime",
                "Search.Players": "0",
            }
            r = self.session.post(url, data=body, timeout=TIMEOUT)
            r.raise_for_status()
            label = offered.get(tag, "")
            out.extend(self._parse_legacy(r.text, course, date, tag, label, url,
                                          multi))
        return out

    def _parse_legacy(self, html: str, course: dict, date: dt.date,
                      tag: str, label: str, url: str,
                      multi: bool) -> list[TeeTime]:
        page = _soup(html)
        # Single-course sites print their one course name in the heading; do
        # not stamp a label there or every slot gets a redundant sub-course.
        out: list[TeeTime] = []
        for node in page.select(".tee-time"):
            tnode = node.select_one(".time-container")
            if tnode is None:
                continue
            m = _TIME_RE.search(tnode.get_text(" ", strip=True))
            if not m:
                continue
            teetime = _to_iso(date, int(m.group(1)), int(m.group(2)), m.group(3))

            detail = node.select_one(".detail")
            text = detail.get_text(" ", strip=True) if detail else ""
            price = None
            pm = _PRICE_RE.search(text)
            if pm:
                price = float(pm.group(1).replace(",", ""))

            # Each player count 1..4 is its own <a>; the ones still bookable
            # carry the digit as text, the rest render empty.
            spots = [int(a.get_text(strip=True))
                     for a in (detail.find_all("a") if detail else [])
                     if a.get_text(strip=True).isdigit()]
            if not spots:
                # Every player-count link is dead: the row is on the sheet but
                # nobody can book it. Publishing it would put a tee time on the
                # site that dead-ends at the club's own "unavailable" page.
                continue
            holes = [9] if re.search(r"\b9 holes?\b", text, re.I) else [18]

            out.append(TeeTime(
                course_slug=course["slug"],
                # display_name wins, matching base_tee_time(): writing
                # the raw name here made every scrape INSERT rows that
                # migrate() then renamed, flapping the card name.
                course_name=course.get("display_name") or course["name"],
                city=course.get("city", ""), platform=self.platform,
                teetime=teetime,
                course_label=label if multi else "",
                state=course.get("state", ""),
                venue_id=course.get("venue_id", course["slug"]),
                source_role=course.get("source_role", "primary"),
                holes=holes,
                open_spots=max(spots) if spots else None,
                price_min=price, price_max=price,
                booking_url=url,
            ))
        return out

    # -- v2 skin -------------------------------------------------------------

    def _fetch_v2(self, course: dict, date: dt.date, site: str) -> list[TeeTime]:
        home = f"https://{V2_HOST}/course/{site}"
        shell = self.session.get(home, timeout=TIMEOUT)
        shell.raise_for_status()
        page = _soup(shell.text)
        # value -> visible course name, read off the radio's own <label> so a
        # multi-course sheet can label its slots (see _parse_v2).
        offered: dict[str, str] = {}
        for i in page.find_all("input", {"name": "selectedCourse"}):
            v = i.get("value")
            if not v:
                continue
            holder = i.find_parent("label") or i.find_next_sibling("label")
            name = holder.get_text(" ", strip=True) if holder else ""
            offered[v] = name or v
        pinned = course["ids"].get("course_tags")
        # A site with a single course renders no radio group at all; fall back
        # to the canonical "<site>-1" only in that case.
        tags = list(pinned or offered or [f"{site}-1"])
        if offered:
            tags = [t for t in tags if t in offered]
        if not tags:
            raise RuntimeError(f"{course['slug']}: no usable teequest course tag")

        # Same rule as legacy: label whenever more than one tag is fetched.
        # The old code hardcoded course_label="" even while iterating several
        # tags, so two courses' 8:00 slots shared one D1 key and one name.
        multi = len(tags) > 1
        out: list[TeeTime] = []
        for tag in tags:
            html = self._v2_search(tag, date, players=1)
            if _SINGLE_BLOCKED_RE.search(html):
                html = self._v2_search(tag, date, players=2)
            label = offered.get(tag, tag) if multi else ""
            out.extend(self._parse_v2(html, course, date, tag, home, label))
        return out

    def _v2_search(self, tag: str, date: dt.date, players: int) -> str:
        r = self.session.get(
            f"https://{V2_HOST}/search/{tag}/{date.isoformat()}",
            params={"selectedPlayers": players, "selectedHoles": 18},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    def _parse_v2(self, html: str, course: dict, date: dt.date,
                  tag: str, url: str, label: str = "") -> list[TeeTime]:
        page = _soup(html)
        out: list[TeeTime] = []
        for node in page.select(".tee-time"):
            raw = (node.get("data-date-time") or "").strip()
            if len(raw) != 12 or not raw.isdigit():
                continue
            teetime = (f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
                       f"T{raw[8:10]}:{raw[10:12]}:00")
            if not teetime.startswith(date.isoformat()):
                continue          # the sheet answered for another day
            try:
                price = float(node.get("data-price"))
            except (TypeError, ValueError):
                price = None
            try:
                spots = int(node.get("data-available"))
            except (TypeError, ValueError):
                spots = None
            if spots == 0:
                continue          # on the sheet, but nothing left to book
            text = node.get_text(" ", strip=True)
            holes = [9] if re.search(r"\b9 holes?\b", text, re.I) else [18]

            out.append(TeeTime(
                course_slug=course["slug"],
                # display_name wins, matching base_tee_time(): writing
                # the raw name here made every scrape INSERT rows that
                # migrate() then renamed, flapping the card name.
                course_name=course.get("display_name") or course["name"],
                city=course.get("city", ""), platform=self.platform,
                teetime=teetime, course_label=label,
                state=course.get("state", ""),
                venue_id=course.get("venue_id", course["slug"]),
                source_role=course.get("source_role", "primary"),
                holes=holes, open_spots=spots,
                price_min=price, price_max=price,
                booking_url=url,
            ))
        return out
