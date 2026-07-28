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

SEVEN HAZARDS THAT SHAPE THIS FILE, all measured:

1. THE GATEWAY HOST IS PER-TENANT AND IS NOT DERIVABLE FROM THE SITE HOST, AND
   THE ORIGIN HEADER MUST AGREE WITH IT. Two separate namespaces, exactly like
   a TeeItUp vanity host versus its kenna alias. Ken McDonald's booking site is
   `kenmcdonald.totaleintegrated.net` but its gateway is
   `courseco-gateway.totaleintegrated.net` (CourseCo is the management
   company); Sun City West's site is `suncitywest.totaleintegrated.net` and its
   gateway is `suncitywest-gateway.totaleintegrated.net`. Guessing either from
   the other fails: every Sun City West call to the CourseCo gateway answered
   400. So BOTH are pinned — `ids.tenant` for the site and `ids.gateway` for
   the API — and both are read off the booking page's own
   `window.__config.apiBaseUrl`, never inferred.

   The Origin header is load-bearing on top of that. A byte-identical request
   to the Sun City West gateway answers 200 from the Sun City West origin and
   400 from the Ken McDonald origin, so the gateway validates Origin against
   its own tenant. Every request sends Origin and Referer for `ids.tenant`.

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

7. THE UNFILTERED QUERY IS SILENTLY CAPPED, SO IT IS NEVER USED FOR ROWS.
   Asking Sun City West for every course at once returned EXACTLY 99 rows on
   each of seven consecutive dates — a server-side cap, not a tee sheet. Asking
   per course returned 49-71 rows for a SINGLE course on those same dates, so
   the combined query was dropping roughly two thirds of the inventory and
   three of the seven courses never appeared in it at all. A whole-tenant fetch
   would therefore have published a truncated sheet and left real courses
   looking permanently dark. `fetch()` always queries one course at a time; the
   unfiltered call is used ONLY to read the `Courses` array, and it logs a
   warning if it ever comes back at the cap again.

A wrong CourseID is SAFE here, unlike golfwithaccess, teesnap and rguest: it
returns an empty list rather than another course's sheet. Every row's own
CourseID is still asserted against the one we asked for, because "safe today"
is not a property to depend on.

Dates beyond BookingDaysInAdvance answer 200 with an empty list rather than an
error, so the far tier costs nothing and `courses_empty` can still deactivate
stale rows.

Course codes are human strings, not ids — Sun City West's are "DEER VALLEY",
"ECHO MESA", "TRAIL RIDGE", spaces and all. They are pinned verbatim.

Registry shapes, both supported. Both pin {tenant, gateway}:

  * ONE venue per course, sharing a tenant (Sun City West's seven, Ken
    McDonald's one). Add {course_id}; course_label stays "" because the venue
    IS the course. This is the normal shape.
  * ONE venue covering several of a tenant's courses. Omit course_id: every
    course on the tenant is fetched SEPARATELY (hazard 7) and labelled from the
    tenant's own Courses list, so same-time slots on two courses stay distinct
    rows in D1.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

from .base import Adapter
from ..models import TeeTime

log = logging.getLogger("teetime")

GATEWAY = "https://{gateway}-gateway.totaleintegrated.net/Booking/Teetimes"
TENANT_HOST = "https://{tenant}.totaleintegrated.net"
BOOKING_PAGE = TENANT_HOST + "/web/tee-times"

# The unfiltered (CourseID empty) query is CAPPED — see hazard 7. This is the
# observed cap, kept only to make the truncation loud in logs if it reappears.
COMBINED_ROW_CAP = 99

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

        gateway = str(ids.get("gateway") or "")
        if not gateway:
            raise ValueError(
                "courseco: registry must pin ids.gateway (the subdomain of "
                "<gateway>-gateway.totaleintegrated.net, read from the booking "
                "page's own window.__config.apiBaseUrl). It is NOT derivable "
                "from ids.tenant — see hazard 1.")

        want = str(ids.get("course_id") or "")
        # Hazard 7: never take rows from an unfiltered query. Always ask for one
        # course at a time, even when this venue is the tenant's only course.
        targets = ([(want, "")] if want
                   else [(c, n) for c, n in self._course_list(tenant, gateway,
                                                              date)])
        if not targets:
            return []
        label = len(targets) > 1

        out: list[TeeTime] = []
        for course_id, display in targets:
            payload = self._search(tenant, gateway, course_id, date)
            for slot in (payload.get("TeeTimeData") or []):
                got = str(slot.get("CourseID") or "")
                # A wrong id returns empty today, but never publish a sheet
                # under a name we did not ask for.
                if got and got != course_id:
                    raise ValueError(
                        f"courseco: asked {tenant} for course {course_id!r} but "
                        f"a row came back as {got!r} — refusing to publish it")
                tt = self._tee_time(course, slot, display if label else "",
                                    tenant, date)
                if tt is not None:
                    out.append(tt)
        return out

    def _course_list(self, tenant: str, gateway: str,
                     date: dt.date) -> list[tuple[str, str]]:
        """Every real course on the tenant, as (CourseValue, CourseDisplay).

        Discovery only. The response's TeeTimeData is discarded because it is
        capped (hazard 7); its `Courses` array is not.
        """
        with _LOCK:
            hit = _COURSES.get(tenant)
        if hit is None:
            payload = self._search(tenant, gateway, "", date)
            hit = [c for c in (payload.get("Courses") or [])
                   if str(c.get("CourseValue") or "") not in ("", ANY_COURSE)]
            with _LOCK:
                _COURSES[tenant] = hit
        return [(str(c["CourseValue"]), str(c.get("CourseDisplay") or ""))
                for c in hit if not c.get("IsClosed")]

    # -- HTTP ----------------------------------------------------------------

    def _headers(self, tenant: str) -> dict[str, str]:
        """Hazard 1: the gateway checks Origin against its own tenant, so these
        two headers are not optional politeness. Measured: the Sun City West
        gateway answers 200 from the Sun City West origin and 400 from another
        tenant's origin, for a byte-identical request."""
        host = TENANT_HOST.format(tenant=tenant)
        return {
            "Origin": host,
            "Referer": host + "/",
            "Accept": "application/json, text/plain, */*",
        }

    def _search(self, tenant: str, gateway: str, course_id: str,
                date: dt.date) -> dict:
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
        data = self.get_json(GATEWAY.format(gateway=gateway),
                             headers=self._headers(tenant),
                             params=params) or {}
        if not course_id and len(data.get("TeeTimeData") or []) >= COMBINED_ROW_CAP:
            log.warning("courseco: %s unfiltered query hit the %d-row cap; "
                        "per-course queries are the only complete read",
                        tenant, COMBINED_ROW_CAP)
        return data

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
