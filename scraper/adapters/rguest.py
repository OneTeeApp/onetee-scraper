"""rGuest Golf adapter — Agilysys' resort booking platform.

Serves two host skins of ONE product: book.rguest.com (platform `rguest`) and
book.onagilysys.com (platform `agilysys`, e.g. Black Desert). Byte-identical
API — token, getAvailableCourses, getAvailableTeeSlots, and the slot/rate shape
all match (verified 2026-08-08 on Black Desert tenant 2434). The host is pinned
in ids.host (default book.rguest.com) so one adapter covers both.

Captured from live traffic (July 2026). Anonymous: no login, no CAPTCHA.

Three calls, all on book.rguest.com:

    GET /wbe-admin-service/generatetoken/v2/tenants/<tenant>
        /propertyId/<property>/appName/NA
    -> { token: "<jwt>", success: true }

    GET /wbe-golf-service/golf/tenants/<tenant>/propertyId/<property>
        /getAvailableCourses?appName=golf
    -> { availableCourses: [ {id, name}, ... ] }

    GET /wbe-golf-service/golf/tenants/<tenant>/propertyId/<property>
        /getAvailableTeeSlots
        ?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&courseId=<id>
        &playerTypeId=0&holes=0&appName=golf&dateTime=YYYY-MM-DDT00:00:00
    -> { availableTeeSlots: [ { date, startTime, endTime, courseId,
                                slots: [ { scheduleDateTime, availability,
                                           holeNumber, teeTimeId,
                                           rateType: [ {name, isPrivate,
                                                        holeType,
                                                        rates:{greenFee,
                                                               cartFee,
                                                               otherFee}} ] } ] } ] }

The token is an ANONYMOUS APPLICATION token, not a user credential: its claims
name it `api_user_<tenant>_prod_us` and it carries no identity. It is minted
with an unauthenticated GET and lives one hour, so it is cached per
(tenant, property) and re-minted a little early. Same shape as the Club Prophet
anonymous token in browser_cps.

FOUR HAZARDS THAT SHAPE THIS FILE, all measured:

1. HTTP 501 IS AN EMPTY SHEET, NOT A FAULT. A date with no published inventory
   answers `501 {"success": false, "errorMessage": "Available Tee Slots result
   is either null or empty."}`. It is sticky per date (3/3 identical retries on
   Wildfire +30) and neighbouring dates serve fine, so it is the API's way of
   saying "zero rows". Left to raise_for_status this would mark the course
   errored on every scrape forever. It is translated to an empty list, which
   also lets the aggregator's `courses_empty` correctly deactivate stale rows.

2. THE PRIVATE RATE IS NOT ALWAYS THE CHEAPEST, so `isPrivate` must be filtered
   before any min/max is taken. We-Ko-Pa's private "Fairmont Princess" rate is
   $94 while its public twilight rate is $79 — a blind max() would publish a
   resort-guest-only price to a walk-up.

3. RESTRICTED PUBLIC RATES SIT ALONGSIDE THE WALK-UP RATE. A slot carries
   "Online Resort" $109, "Online AZ ID" $74 (Arizona residents) and "Online
   WeKoPass" $64 (pass holders) — all `isPrivate: false`. Publishing the min
   would advertise a price most golfers cannot get, so we publish the HIGHEST
   non-private rate: every one of these is a discount off the unrestricted rate.
   This is safe across the twilight boundary because twilight slots carry ONLY
   twilight rates (measured: 16:20 offers "Online Resort Twilight" $79 and no
   full-price rate), so the max is that slot's true walk-up price, not the
   morning rack rate. Same discipline as golfwithaccess publishing "Public".

4. A WRONG courseId SUCCEEDS WITH ANOTHER COURSE'S SHEET rather than failing,
   exactly like golfwithaccess and teesnap. Every returned group carries its
   own `courseId`, so it is asserted against the one we asked for and the fetch
   raises rather than publish another course under our name.

Registry shapes, both supported:

  * ONE venue, MANY courses (We-Ko-Pa = Cholla + Saguaro; Camelback = Ambiente
    + Padre). Pin only {tenant, property}; every course on the property is
    fetched and labelled with its own name via course_label, so same-time slots
    on the two courses stay distinct rows in D1.
  * ONE venue, ONE course of a shared property (Wildfire's Faldo and Palmer are
    two separate registry venues on tenant 2418). Pin {tenant, property,
    course_id}; course_label stays "" because the venue IS the course.

`scheduleDateTime` is already local wall-clock for the property, so it is used
verbatim — no timezone conversion. The `timeZone` header is what the service
expects to resolve the business date; all four current venues are Arizona, and
it is overridable per course via ids["timezone"].
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import requests

from .base import Adapter
from ..models import TeeTime

DEFAULT_HOST = "book.rguest.com"


def _base(host: str) -> str:
    return f"https://{host}"


def _token_url(host: str, tenant: str, prop: str) -> str:
    return (_base(host) + "/wbe-admin-service/generatetoken/v2/tenants/"
            f"{tenant}/propertyId/{prop}/appName/NA")


def _golf(host: str, tenant: str, prop: str) -> str:
    return _base(host) + f"/wbe-golf-service/golf/tenants/{tenant}/propertyId/{prop}"


def _booking_page(host: str, tenant: str, prop: str) -> str:
    return _base(host) + f"/onecart/golf/courses/{tenant}/{prop}"

DEFAULT_TZ = "America/Phoenix"
_TOKEN_TTL = 45 * 60          # tokens live ~1h; re-mint early

# (tenant, property) -> (token, expires_at_epoch). Shared across adapter
# instances because the aggregator builds one per course and the whole fleet
# would otherwise mint a token per course per date.
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}
_COURSES: dict[tuple[str, str], list[dict]] = {}
_LOCK = threading.Lock()


class RGuestAdapter(Adapter):
    platform = "rguest"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        tenant, prop = str(ids.get("tenant") or ""), str(ids.get("property") or "")
        if not tenant or not prop:
            raise ValueError(
                "rguest: registry must pin ids.tenant and ids.property "
                "(from book.rguest.com/onecart/golf/courses/<tenant>/<property>)")

        host = str(ids.get("host") or DEFAULT_HOST)
        tz = ids.get("timezone") or DEFAULT_TZ
        token = self._token(host, tenant, prop)

        pinned = ids.get("course_id")
        if pinned is not None:
            targets = [(int(pinned), "")]          # venue IS the course
        else:
            # Whole property: label each sub-course so D1 keeps them distinct.
            targets = [(int(c["id"]), str(c.get("name") or ""))
                       for c in self._courses(host, tenant, prop, token, tz)]
        if not targets:
            return []

        out: list[TeeTime] = []
        for course_id, label in targets:
            for group in self._slots(host, tenant, prop, token, tz, course_id, date):
                # Hazard 4: never publish another course's sheet as ours.
                got = group.get("courseId")
                if got is not None and int(got) != course_id:
                    raise ValueError(
                        f"rguest: asked for courseId {course_id} but the sheet "
                        f"returned {got} — refusing to publish it")
                for s in (group.get("slots") or []):
                    tt = self._tee_time(course, s, label, host, tenant, prop,
                                        course_id, date)
                    if tt is not None:
                        out.append(tt)
        return out

    # -- HTTP ----------------------------------------------------------------

    def _token(self, host: str, tenant: str, prop: str) -> str:
        key = (host, tenant, prop)
        with _LOCK:
            hit = _TOKENS.get(key)
            if hit and hit[1] > time.time():
                return hit[0]
        data = self.get_json(_token_url(host, tenant, prop))
        token = (data or {}).get("token")
        if not token:
            raise ValueError(f"rguest: no token minted for {tenant}/{prop}")
        with _LOCK:
            _TOKENS[key] = (token, time.time() + _TOKEN_TTL)
        return token

    def _headers(self, token: str, tz: str, date: dt.date) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "timeZone": tz,
            "propertyDTTM": f"{date.isoformat()}T00:00:00",
            "Accept": "application/json, text/plain, */*",
        }

    def _courses(self, host: str, tenant: str, prop: str, token: str,
                 tz: str) -> list[dict]:
        key = (host, tenant, prop)
        with _LOCK:
            hit = _COURSES.get(key)
        if hit is not None:
            return hit
        data = self.get_json(
            _golf(host, tenant, prop) + "/getAvailableCourses",
            headers=self._headers(token, tz, dt.date.today()),
            params={"appName": "golf"})
        courses = [c for c in ((data or {}).get("availableCourses") or [])
                   if c.get("id") is not None]
        if not courses:
            # "Unknown" is not "no courses". A transient empty/garbled 200
            # used to be cached for the run, after which every un-pinned venue
            # on the property returned a clean empty day — which sync then
            # deactivated. Unknown must raise, and an empty answer must never
            # be remembered.
            raise RuntimeError(
                f"rguest: getAvailableCourses returned nothing for "
                f"{tenant}/{prop} — refusing to publish an empty day off it")
        with _LOCK:
            _COURSES[key] = courses
        return courses

    def _slots(self, host: str, tenant: str, prop: str, token: str, tz: str,
               course_id: int, date: dt.date) -> list[dict]:
        url = _golf(host, tenant, prop) + "/getAvailableTeeSlots"
        params = {
            "fromDate": date.isoformat(),
            "toDate": date.isoformat(),
            "courseId": course_id,
            "playerTypeId": 0,      # 0 = every player type
            "holes": 0,             # 0 = 9 and 18
            "appName": "golf",
            "dateTime": f"{date.isoformat()}T00:00:00",
        }
        try:
            data = self.get_json(url, headers=self._headers(token, tz, date),
                                 params=params)
        except requests.HTTPError as e:
            # Hazard 1: 501 is this API's "no rows", not a server fault.
            if self._is_empty_501(e):
                return []
            raise
        return (data or {}).get("availableTeeSlots") or []

    @staticmethod
    def _is_empty_501(e: requests.HTTPError) -> bool:
        r = getattr(e, "response", None)
        if r is None or r.status_code != 501:
            return False
        try:
            body = r.json()
        except ValueError:
            return False
        return "null or empty" in str(body.get("errorMessage", "")).lower()

    # -- parsing -------------------------------------------------------------

    def _tee_time(self, course: dict, slot: dict, label: str, host: str,
                  tenant: str, prop: str, course_id: int,
                  date: dt.date) -> TeeTime | None:
        teetime = self._iso(slot.get("scheduleDateTime"))
        if teetime is None:
            return None
        public = [r for r in (slot.get("rateType") or []) if not r.get("isPrivate")]
        price = self._walkup_price(public)          # hazards 2 + 3
        tt = self.base_tee_time(
            course,
            teetime=teetime,
            course_label=label,
            holes=self._holes(public),
            open_spots=self._int(slot.get("availability")),
            price_min=price, price_max=price,
            raw=slot,
        )
        tt.booking_url = self._slot_url(course, host, tenant, prop, course_id, date)
        return tt

    @staticmethod
    def _iso(value: str | None) -> str | None:
        """'2026-07-30T11:50:00' -> same, validated. Already property-local."""
        if not value:
            return None
        try:
            return dt.datetime.fromisoformat(str(value)[:19]).isoformat(
                timespec="seconds")
        except ValueError:
            return None

    @staticmethod
    def _int(v: Any) -> int | None:
        return int(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _holes(public_rates: list[dict]) -> list[int]:
        got = {int(r["holeType"]) for r in public_rates
               if isinstance(r.get("holeType"), (int, float))}
        return sorted(h for h in got if h in (9, 18))

    @staticmethod
    def _walkup_price(public_rates: list[dict]) -> float | None:
        """What a golfer with no affiliation pays for THIS slot.

        Private rates are already excluded by the caller (hazard 2). Among the
        rest, every named rate is a restricted discount off the unrestricted
        one, so the maximum is the walk-up price (hazard 3).
        """
        totals: list[float] = []
        for r in public_rates:
            fees = r.get("rates") or {}
            parts = [fees.get(k) for k in ("greenFee", "cartFee", "otherFee")]
            nums = [float(p) for p in parts if isinstance(p, (int, float))]
            if nums:
                totals.append(sum(nums))
        return max(totals) if totals else None

    @staticmethod
    def _slot_url(course: dict, host: str, tenant: str, prop: str,
                  course_id: int, date: dt.date) -> str:
        base = course.get("booking_url") or _booking_page(host, tenant, prop)
        base = base.split("?")[0]
        return f"{base}?date={date.isoformat()}&id={course_id}"
