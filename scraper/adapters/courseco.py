"""CourseCo adapter — Total-e Integrated's replacement booking platform.

Captured from live traffic 2026-07-28. Anonymous: no login, no token, no
CAPTCHA. One GET returns the whole day.

    GET https://courseco-gateway.totaleintegrated.net/Booking/Teetimes
        ?TeeTimeDate=YYYY-MM-DD&CourseID=<code|empty>&StartTime=05:00
        &EndTime=21:00&NumOfPlayers=0&Holes=-1&IsNineHole=-1&StartPrice=0
        &EndPrice=&CartIncluded=false&SpecialsOnly=0&IsClosest=0&PlayerIDs=
        &DateFilterChange=false&DateFilterChangeNoSearch=false
        &SearchByGroups=true&IsPrepaidOnly=0&CourseFavoritesChecked=true
        &QueryStringFilters=null&IsInitTeeTimeRequest=false
    -> { Courses: [ {CourseValue, CourseDisplay, IsEnabled, IsClosed}, ... ],
         BookingDaysInAdvance: 30,
         TeeTimeData: [ { Title, SubTitle, DisplayTime, Time, TTDate,
                          CourseID, CourseName, AvailableSlot, Allow9,
                          Allow18, HoleOptions, PerPlayerCost,
                          GolfPrice18, GolfPowerCartPrice18,
                          GolfPrice9,  GolfPowerCartPrice9,
                          DiscountGolfPrice18, ..., RequireGolfCart,
                          IsCartIncluded, GreenFee, CartFee, Total },
                        ... ] }

WHY THIS FILE EXISTS. Ken McDonald (Tempe) is the first course we have seen
migrate off the legacy Total-e tenant — playkenmcdonald.totaleintegrated.com
now answers Cloudflare 525 (origin SSL dead), while the course is very much
selling a full sheet at kenmcdonald.totaleintegrated.NET. The old host being
down looked exactly like a dark course from the inside. Expect the other
totale tenants to follow, which is why this is a platform adapter and not a
one-course patch.

SIX HAZARDS THAT SHAPE THIS FILE, all measured:

1. THE TENANT COMES FROM THE ORIGIN HEADER, NOT FROM ANY PARAMETER. The
   gateway host is shared by every club. Asking it for CourseID
   "CAMPUSCOMMONS" from the Ken McDonald origin returns zero rows and a
   `Courses` list of exactly ANY/KENMCDONALD — the tenant was never in
   question, only the course filter. So every request sends Origin and
   Referer for the tenant's own booking host, and `ids.tenant` is what pins
   the venue. Its CORS policy is scoped the same way: a browser call from any
   other origin fails outright.

2. -0.01 IS A SENTINEL, NOT A PRICE. Every slot carries
   DiscountGolfPrice18 = -0.01 when no discount applies, and 0 in the
   PullCart fields when a pull cart is not offered. A naive min() across the
   price fields publishes MINUS ONE CENT. Only strictly-positive values are
   considered.

3. GreenFee, CartFee, SalesTax AND Total ARE ALL ZERO ON THE SEARCH
   RESPONSE. They are filled in after the golfer picks holes and a cart, so
   the obvious-looking "Total" field would publish $0 for every tee time. The
   real numbers live in the Golf*Price9/18 matrix and in PerPlayerCost.

4. Holes IS ALWAYS -1. It means "either", not "unknown" and not "nine". The
   bookable hole counts are in HoleOptions[].IsEnabled (with Allow9/Allow18
   as the fallback), and that is what we publish.

5. PerPlayerCost IS THE 18-HOLE RIDING PRICE. Measured on all 49 slots of a
   sample day: PerPlayerCost == GolfPrice18 + GolfPowerCartPrice18, exactly,
   every time. It is the number the booking card shows the golfer, so it is
   published as price_max — but RequireGolfCart is 0 here, so walking really
   is bookable and the walking green fee is a price a golfer can actually
   get. Publishing only PerPlayerCost would overstate the floor by the cart
   fee. So price_min is the cheapest strictly-positive walking green fee
   among the hole counts the slot allows, and price_max is PerPlayerCost.
   When a course DOES require a cart, the walking fees are excluded and the
   range collapses onto the riding price.

6. AvailableSlot IS A RANGE STRING ("2-4"), NOT A COUNT. Its upper bound is
   how many players can still be booked into the slot; its lower bound is the
   course's minimum group size. open_spots takes the upper bound.

A wrong CourseID is SAFE here, unlike golfwithaccess, teesnap and rguest: it
returns an empty list rather than another course's sheet. Every row's own
CourseID is still asserted against the one we asked for, because "safe today"
is not a property to depend on.

Dates beyond BookingDaysInAdvance answer 200 with an empty list rather than an
error, so the far tier costs nothing and `courses_empty` can still deactivate
stale rows.

Registry shapes, both supported:

  * ONE venue, ONE course (Ken McDonald). Pin {tenant} and optionally
    {course_id}; course_label stays "" because the venue IS the course.
  * ONE tenant, MANY courses. Pin only {tenant}: a single request returns
    every course's slots, and each row is labelled from the tenant's own
    Courses list so same-time slots stay distinct rows in D1. Pin
    {tenant, course_id} instead when two venues share one tenant.
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from .base import Adapter
from ..models import TeeTime

GATEWAY = "https://courseco-gateway.totaleintegrated.net/Booking/Teetimes"
TENANT_HOST = "https://{tenant}.totaleintegrated.net"
BOOKING_PAGE = TENANT_HOST + "/web/tee-times"

# The search window the booking page itself uses. Narrower than the tee sheet
# would ever be, so it never clips a real slot.
START_TIME = "05:00"
END_TIME = "21:00"

# CourseValue of the synthetic "All Courses" entry — never a real course.
ANY_COURSE = "ANY"

# tenant -> [{CourseValue, CourseDisplay}, ...]. Shared across adapter
# instances: the aggregator builds one adapter per course, and a multi-course
# tenant would otherwise re-derive the same list once per course per date.
_COURSES: dict[str, list[dict]] = {}
_LOCK = threading.Lock()


class CourseCoAdapter(Adapter):
    platform = "courseco"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids") or {}
        tenant = str(ids.get("tenant") or "")
        if not tenant:
            raise ValueError(
                "courseco: registry must pin ids.tenant (the subdomain of "
                "<tenant>.totaleintegrated.net that fronts the booking page)")

        want = str(ids.get("course_id") or "")
        payload = self._search(tenant, want, date)
        self._remember_courses(tenant, payload)

        rows = payload.get("TeeTimeData") or []
        label_for = self._labeller(tenant, want)

        out: list[TeeTime] = []
        for slot in rows:
            got = str(slot.get("CourseID") or "")
            # Hazard: a wrong id returns empty today, but never publish a
            # sheet under a name we did not ask for.
            if want and got and got != want:
                raise ValueError(
                    f"courseco: asked {tenant} for course {want!r} but a row "
                    f"came back as {got!r} — refusing to publish it")
            tt = self._tee_time(course, slot, label_for(got), tenant, date)
            if tt is not None:
                out.append(tt)
        return out

    # -- HTTP ----------------------------------------------------------------

    def _headers(self, tenant: str) -> dict[str, str]:
        """Hazard 1: the tenant is identified by where the request claims to
        come from, so these two headers are not optional politeness."""
        host = TENANT_HOST.format(tenant=tenant)
        return {
            "Origin": host,
            "Referer": host + "/",
            "Accept": "application/json, text/plain, */*",
        }

    def _search(self, tenant: str, course_id: str, date: dt.date) -> dict:
        params = {
            "IsInitTeeTimeRequest": "false",
            "TeeTimeDate": date.isoformat(),
            "CourseID": course_id,        # empty = every course on the tenant
            "StartTime": START_TIME,
            "EndTime": END_TIME,
            "NumOfPlayers": "0",          # 0 = any group size
            "Holes": "-1",                # -1 = 9 and 18 (hazard 4)
            "IsNineHole": "-1",
            "StartPrice": "0",
            "EndPrice": "",
            "CartIncluded": "false",
            "SpecialsOnly": "0",
            "IsClosest": "0",
            "PlayerIDs": "",
            "DateFilterChange": "false",
            "DateFilterChangeNoSearch": "false",
            "SearchByGroups": "true",
            "IsPrepaidOnly": "0",
            "CourseFavoritesChecked": "true",
            "QueryStringFilters": "null",
        }
        data = self.get_json(GATEWAY, headers=self._headers(tenant),
                             params=params)
        return data or {}

    def _remember_courses(self, tenant: str, payload: dict) -> None:
        listed = [c for c in (payload.get("Courses") or [])
                  if str(c.get("CourseValue") or "") not in ("", ANY_COURSE)]
        if listed:
            with _LOCK:
                _COURSES[tenant] = listed

    def _labeller(self, tenant: str, pinned: str):
        """Return f(course_id) -> sub-course label.

        A venue that IS the course gets "" so D1 keeps one row per time. A
        tenant fronting several courses labels each row with that course's own
        display name, so 8:00 on two courses stays two rows.
        """
        if pinned:
            return lambda _cid: ""
        with _LOCK:
            listed = list(_COURSES.get(tenant) or [])
        if len(listed) <= 1:
            return lambda _cid: ""
        names = {str(c.get("CourseValue")): str(c.get("CourseDisplay") or "")
                 for c in listed}
        return lambda cid: names.get(str(cid), "")

    # -- parsing -------------------------------------------------------------

    def _tee_time(self, course: dict, slot: dict, label: str, tenant: str,
                  date: dt.date) -> TeeTime | None:
        teetime = self._iso(slot, date)
        if teetime is None:
            return None
        holes = self._holes(slot)
        lo, hi = self._prices(slot, holes)
        tt = self.base_tee_time(
            course,
            teetime=teetime,
            course_label=label,
            holes=holes,
            open_spots=self._open_spots(slot.get("AvailableSlot")),
            price_min=lo, price_max=hi,
            raw=slot,
        )
        tt.booking_url = (course.get("booking_url")
                          or BOOKING_PAGE.format(tenant=tenant))
        return tt

    @staticmethod
    def _iso(slot: dict, date: dt.date) -> str | None:
        """'08/10/2026' + '10:33:00:000' -> '2026-08-10T10:33:00'.

        Already course-local wall clock, so it is used verbatim — no timezone
        conversion. Time is HH:MM:SS:mmm (colon, not dot, before the
        milliseconds), which datetime cannot parse, so the clock part is taken
        positionally and DisplayTime ("10:33 AM") is the fallback.
        """
        day = date
        raw_day = str(slot.get("TTDate") or "")
        if raw_day:
            try:
                day = dt.datetime.strptime(raw_day[:10], "%m/%d/%Y").date()
            except ValueError:
                pass

        parts = str(slot.get("Time") or "").split(":")
        if len(parts) >= 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                s = int(parts[2]) if len(parts) > 2 else 0
                return dt.datetime.combine(
                    day, dt.time(h, m, s)).isoformat(timespec="seconds")
            except ValueError:
                pass

        for key in ("DisplayTime", "Title"):
            try:
                t = dt.datetime.strptime(
                    str(slot.get(key) or "").strip().upper(), "%I:%M %p").time()
            except ValueError:
                continue
            return dt.datetime.combine(day, t).isoformat(timespec="seconds")
        return None

    @staticmethod
    def _holes(slot: dict) -> list[int]:
        """Hazard 4: Holes is always -1; the truth is in HoleOptions."""
        got: set[int] = set()
        for opt in (slot.get("HoleOptions") or []):
            if opt.get("IsEnabled") is False:
                continue
            try:
                got.add(int(opt.get("Value")))
            except (TypeError, ValueError):
                continue
        if not got:
            if slot.get("Allow9"):
                got.add(9)
            if slot.get("Allow18"):
                got.add(18)
        return sorted(h for h in got if h in (9, 18))

    @staticmethod
    def _open_spots(available: Any) -> int | None:
        """Hazard 6: '2-4' is a group-size range; its top is what is left."""
        if isinstance(available, (int, float)):
            return int(available)
        best: int | None = None
        for part in str(available or "").split("-"):
            part = part.strip()
            if part.isdigit():
                n = int(part)
                best = n if best is None else max(best, n)
        return best

    @classmethod
    def _prices(cls, slot: dict, holes: list[int]) -> tuple[float | None,
                                                            float | None]:
        """Hazards 2, 3 and 5: the honest range a golfer can actually pay."""
        riding = cls._positive(slot.get("PerPlayerCost"))

        walking: list[float] = []
        if not slot.get("RequireGolfCart") and not slot.get("IsCartIncluded"):
            for h in (holes or [9, 18]):
                fee = cls._positive(slot.get(f"GolfPrice{h}"))
                if fee is not None:
                    walking.append(fee)

        if riding is None and not walking:
            # Last resort: rebuild the riding price from its parts.
            for h in (holes or [18]):
                green = cls._positive(slot.get(f"GolfPrice{h}"))
                cart = cls._positive(slot.get(f"GolfPowerCartPrice{h}"))
                if green is not None:
                    walking.append(green + (cart or 0.0))
            if not walking:
                return None, None

        candidates = walking + ([riding] if riding is not None else [])
        return min(candidates), max(candidates)

    @staticmethod
    def _positive(value: Any) -> float | None:
        """Hazards 2 + 3: -0.01 means 'no discount', 0 means 'not offered',
        and the Total/GreenFee/CartFee fields are 0 until the golfer picks
        options. None of those are prices."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if value > 0 else None
