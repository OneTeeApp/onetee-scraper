"""Club Prophet (CPS Golf, *.cps.golf) — public anonymous online-reservation API.

Captured live July 2026 from indianpeaks.cps.golf. Earlier notes here claimed a
confidential client secret was required; that was wrong. The public reservation
SPA authenticates every anonymous visitor with a SHORT-LIVED token minted from a
PUBLIC client id (no secret, no login):

  1. POST https://<tenant>.cps.golf/identityapi/myconnect/token/short
        Content-Type: application/x-www-form-urlencoded
        body: client_id=onlinereswebshortlived
     -> { access_token: <bearer> }        # anonymous, ~short lived

  All /onlineres/onlineapi calls then need that bearer PLUS a set of static,
  public component headers the SPA always sends (client-id, x-productid=1,
  x-componentid=1, x-siteid=1, x-moduleid=7, x-terminalid=3, x-websiteid=<tenant
  guid>, ...). Missing them yields 400 "Invalid componentid request header".

  2. GET  .../onlinereservation/OnlineCourses          -> course list (ids)
  3. POST .../onlinereservation/RegisterTransactionId
        body: {"transactionId": "<client-generated-guid>"}   -> true
  4. GET  .../onlinereservation/TeeTimes?searchDate=<Ddd Mmm DD YYYY>
        &courseIds=<csv>&transactionId=<same guid>&teeOffTimeMin=0
        &teeOffTimeMax=23&holes=0&numberOfPlayer=0&classCode=R&searchType=1&...
     -> { content: [ { startTime, courseId, courseName, holes, is9HoleOnly,
                       availableParticipantNo, shItemPrices[...], ... }, ... ] }

Registry ids: {"tenant": "<sub>"} is enough (courseIds + websiteId are
discovered at runtime via OnlineCourses); optional {"website_id", "course_ids"}
override discovery if a tenant ever needs it pinned.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from .base import Adapter, USER_AGENT
from ..models import TeeTime

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
_TOKEN_CLIENT = "onlinereswebshortlived"


class ClubProphetAdapter(Adapter):
    platform = "clubprophet"

    # -- low-level helpers ---------------------------------------------------

    def _token(self, base: str) -> str:
        r = self.session.post(
            f"{base}/identityapi/myconnect/token/short",
            data={"client_id": _TOKEN_CLIENT},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            timeout=20)
        r.raise_for_status()
        tok = r.json()
        token = tok.get("access_token") or tok.get("token")
        if not token:
            raise RuntimeError("Club Prophet: no access_token from token/short")
        return token

    def _headers(self, token: str, website_id: str) -> dict:
        # Static public component identifiers the onlineresweb SPA always sends.
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "client-id": "onlineresweb",
            "x-terminalid": "3",
            "x-websiteid": website_id or _ZERO_GUID,
            "x-ismobile": "false",
            "x-productid": "1",
            "x-componentid": "1",
            "x-siteid": "1",
            "x-moduleid": "7",
            "x-timezoneid": "America/Denver",
            "x-timezone-offset": "360",
            "x-requestid": str(uuid.uuid4()),
        }

    def _discover(self, base: str, tenant: str, token: str) -> tuple[list[int], str | None]:
        """Return (courseIds, websiteId) from the tenant's bootstrap config.

        GetAllOptions/<tenant> is the SPA's first data call and needs only the
        anonymous token (no websiteId) — it returns webSiteId plus courseOptions.
        OnlineCourses can't be used for discovery because it *requires* the real
        websiteId (a zero guid returns an empty list).
        """
        headers = self._headers(token, _ZERO_GUID)
        r = self.session.get(
            f"{base}/onlineres/onlineapi/api/v1/onlinereservation/GetAllOptions/{tenant}",
            headers=headers, timeout=20)
        r.raise_for_status()
        body = r.json()
        opts = body.get("reservationOptions") or {}
        website_id = body.get("webSiteId") or opts.get("webSiteId")
        ids: list[int] = []
        for c in (body.get("courseOptions") or []):
            cid = c.get("courseId", c.get("id"))
            if cid is not None and int(cid) >= 0 and int(cid) not in ids:
                ids.append(int(cid))
        return ids, website_id

    # -- parsing -------------------------------------------------------------

    @staticmethod
    def _prices(slot: dict) -> list[float]:
        out: list[float] = []
        def _num(v):
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None
        r = _num(slot.get("defaultBookingRate"))
        if r:
            out.append(r)
        for item in (slot.get("shItemPrices") or []):
            if isinstance(item, dict):
                for k in ("price", "amount", "rate", "greenFee", "total"):
                    n = _num(item.get(k))
                    if n:
                        out.append(n)
                        break
            else:
                n = _num(item)
                if n:
                    out.append(n)
        return out

    @staticmethod
    def _holes(slot: dict) -> list[int]:
        if slot.get("is9HoleOnly"):
            return [9]
        for k in ("holes", "defaultHoles"):
            v = slot.get(k)
            if v in (9, 18):
                return [int(v)]
        disp = str(slot.get("holesDisplay") or "")
        found = sorted({int(x) for x in ("9", "18") if x in disp})
        return found or [18]

    # -- main ----------------------------------------------------------------

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course.get("ids", {})
        tenant = ids.get("tenant")
        if not tenant:
            raise ValueError(f"{course['slug']}: no Club Prophet tenant")
        base = f"https://{tenant}.cps.golf"
        api = f"{base}/onlineres/onlineapi/api/v1/onlinereservation"

        token = self._token(base)
        website_id = ids.get("website_id")
        course_ids = ids.get("course_ids")

        # Discover courseIds / websiteId when not pinned in the registry.
        if not course_ids or not website_id:
            try:
                disc_ids, disc_web = self._discover(base, tenant, token)
            except Exception:
                disc_ids, disc_web = [], None
            course_ids = course_ids or disc_ids
            website_id = website_id or disc_web
        if not course_ids:
            raise RuntimeError(f"{course['slug']}: could not resolve Club Prophet "
                               "courseIds (OnlineCourses discovery failed)")

        headers = self._headers(token, website_id or _ZERO_GUID)

        # Register a client-generated transaction id, then query the tee sheet.
        txid = str(uuid.uuid4())
        self.session.post(f"{api}/RegisterTransactionId",
                          json={"transactionId": txid},
                          headers={**headers, "Content-Type": "application/json"},
                          timeout=20)

        course_csv = ",".join(str(c) for c in course_ids)
        params = {
            "searchDate": date.strftime("%a %b %d %Y"),
            "holes": 0, "numberOfPlayer": 0, "courseIds": course_csv,
            "searchTimeType": 0, "transactionId": txid,
            "teeOffTimeMin": 0, "teeOffTimeMax": 23, "isChangeTeeOffTime": "true",
            "teeSheetSearchView": 5, "classCode": "R", "defaultOnlineRate": "N",
            "isUseCapacityPricing": "false", "memberStoreId": 1, "searchType": 1,
        }
        r = self.session.get(f"{api}/TeeTimes", params=params, headers=headers,
                             timeout=20)
        r.raise_for_status()
        body = r.json()
        slots = body.get("content", []) if isinstance(body, dict) else (body or [])

        out: list[TeeTime] = []
        for s in slots:
            t = s.get("startTime")
            if not t:
                continue
            prices = self._prices(s)
            spots = s.get("availableParticipantNo")
            out.append(self.base_tee_time(
                course,
                teetime=str(t),
                holes=self._holes(s),
                open_spots=int(spots) if isinstance(spots, (int, float)) else None,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                raw={"course_name": s.get("courseName", course["name"]),
                     "courseId": s.get("courseId")},
            ))
        return out
