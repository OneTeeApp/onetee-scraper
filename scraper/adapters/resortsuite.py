"""ResortSuite (RSWS) adapter — anonymous SOAP, no auth (captured July 2026).

The Omni Homestead's booking SPA talks to a ResortSuite web service:

  POST https://<host>/wso2wsas/services/RSWS?action=FetchGolfTeeSheet
       Content-Type: text/xml;charset=UTF-8

  <soapenv:Envelope xmlns:g="http://www.resortsuite.com/RSWS/v1/Golf/Types"
                    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
    <soapenv:Body>
      <g:FetchGolfTeeSheetRequest>
        <CourseId>CAS</CourseId>       <- the whole ballgame; see below
        <Date>2026-08-06</Date>
        <GroupCode>undefined</GroupCode>
        <Version>2</Version>
        <WebFolioId>0</WebFolioId>     <- 0 works; no session needed
      </g:FetchGolfTeeSheetRequest>
    </soapenv:Body>
  </soapenv:Envelope>

  -> <TeeTimes><TeeTime>
       <DateTime>2026-08-06080000</DateTime>   (YYYY-MM-DD then HHMMSS)
       <Time>08:00 AM</Time>
       <TimeLocked>N</TimeLocked>
       <SlotsAvailable>2</SlotsAvailable>
       <Rates><Rate><ItemCategory>GREENSFEE</ItemCategory>
                    <Price>303.00</Price>
                    <PriceWithSurcharges>296.00</PriceWithSurcharges>
       ...

CourseId IS honoured — the earlier "both courses are the same sheet" was wrong.

A previous investigation concluded that the Cascades and Old Course routes both
sent `CourseCode=CAS` and returned byte-identical responses, and the Old Course
was held as unproven on that basis. That was true of what the *SPA* sent: it
keeps the selected course in component state and ignores the route, so clicking
through to the Old Course re-sent Cascades. It is not true of the service.

Driving CourseId directly, on 2026-08-06, the two disagree in a way no shared
sheet could fake:

    CAS  54 times, 10-minute intervals  (08:00, 08:10, 08:20, 08:30, 08:50 ...)
    OLD  68 times,  8-minute intervals  (08:00, 08:08, 08:16, 08:24, 08:32 ...)

Different tee intervals are a course configuration, not a rendering artifact. So
both venues are real and each gets its own registry row with its own CourseId.
The lesson is narrower than "the old note was wrong": **a SPA that ignores its
own route will lie to you about which resource you asked for — drive the
service, not the page.**

PRIVACY — DO NOT READ THE PLAYER NAME FIELDS.

Every TeeTime carries P1FirstName/P1LastName .. P4FirstName/P4LastName naming
the golfers already booked into that slot. On this tenant they came back masked
as "PRIVATE MEMBER", but that is a per-property setting and cannot be relied on;
another ResortSuite property may return real names. Those fields are other
people's personal data, they are worthless to us, and the open-slot count is
already given directly by SlotsAvailable. This adapter therefore parses only
DateTime / Time / TimeLocked / SlotsAvailable / Rates and never touches P*Name.
Do not "improve" it by inferring occupancy from the name fields.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .base import Adapter, TIMEOUT
from ..models import TeeTime

PATH = "/wso2wsas/services/RSWS"

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n'
    '<soapenv:Envelope '
    'xmlns:g="http://www.resortsuite.com/RSWS/v1/Golf/Types" '
    'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soapenv:Body><g:FetchGolfTeeSheetRequest>'
    '<CourseId>{course}</CourseId>'
    '<Date>{date}</Date>'
    '<GroupCode>undefined</GroupCode>'
    '<Version>2</Version>'
    '<WebFolioId>0</WebFolioId>'
    '</g:FetchGolfTeeSheetRequest></soapenv:Body></soapenv:Envelope>'
)

_TEETIME_RE = re.compile(r"<TeeTime>(.*?)</TeeTime>", re.S)
_DATETIME_RE = re.compile(r"<DateTime>\s*(\d{4}-\d{2}-\d{2})(\d{2})(\d{2})\d{2}\s*</DateTime>")
_LOCKED_RE = re.compile(r"<TimeLocked>\s*([YN])\s*</TimeLocked>")
_SLOTS_RE = re.compile(r"<SlotsAvailable>\s*(\d+)\s*</SlotsAvailable>")
_RATE_RE = re.compile(r"<Rate>(.*?)</Rate>", re.S)
_CATEGORY_RE = re.compile(r"<ItemCategory>\s*([^<]*?)\s*</ItemCategory>")
_PRICE_RE = re.compile(r"<Price>\s*([\d.]+)\s*</Price>")
_SURCHARGE_RE = re.compile(r"<PriceWithSurcharges>\s*([\d.]+)\s*</PriceWithSurcharges>")
_HOLES_RE = re.compile(r"\b9\b")


class ResortSuiteAdapter(Adapter):
    platform = "resortsuite"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        host = ids.get("host")
        course_id = ids.get("course_id")
        if not host or not course_id:
            raise ValueError(f"{course['slug']}: resortsuite needs host + course_id")

        body = _ENVELOPE.format(course=course_id, date=date.isoformat())
        r = self.session.post(
            f"https://{host}{PATH}", params={"action": "FetchGolfTeeSheet"},
            data=body.encode("utf-8"), timeout=TIMEOUT,
            headers={"Content-Type": "text/xml;charset=UTF-8",
                     "Accept": "text/xml, */*"},
        )
        r.raise_for_status()
        xml = r.text
        if "FetchGolfTeeSheetResponse" not in xml:
            raise RuntimeError(
                f"{course['slug']}: no tee-sheet response from {host} "
                f"({len(xml)} bytes) — SOAP fault or wrong action")

        out: list[TeeTime] = []
        for rec in _TEETIME_RE.findall(xml):
            m = _DATETIME_RE.search(rec)
            if not m:
                continue
            day, hh, mm = m.group(1), m.group(2), m.group(3)
            if day != date.isoformat():
                continue           # the sheet answered for another day
            lock = _LOCKED_RE.search(rec)
            if lock and lock.group(1) == "Y":
                continue           # held by the shop, not sellable
            sm = _SLOTS_RE.search(rec)
            spots = int(sm.group(1)) if sm else 0
            if spots <= 0:
                continue           # on the sheet, nothing left to book

            prices: list[float] = []
            holes = 18
            for rate in _RATE_RE.findall(rec):
                cat = _CATEGORY_RE.search(rate)
                if cat and cat.group(1).upper() != "GREENSFEE":
                    continue       # cart/rental lines are not the green fee
                # PriceWithSurcharges is what the golfer is actually quoted and
                # can be LOWER than Price (303.00 vs 296.00 here), so prefer it
                # and fall back rather than assuming it is a markup.
                p = _SURCHARGE_RE.search(rate) or _PRICE_RE.search(rate)
                if p:
                    prices.append(float(p.group(1)))
                if _HOLES_RE.search(rate.split("<ItemName>")[-1].split("</ItemName>")[0]
                                    if "<ItemName>" in rate else ""):
                    holes = 9

            out.append(TeeTime(
                course_slug=course["slug"], course_name=course["name"],
                city=course.get("city", ""), platform=self.platform,
                teetime=f"{day}T{hh}:{mm}:00", course_label="",
                state=course.get("state", ""),
                venue_id=course.get("venue_id", course["slug"]),
                source_role=course.get("source_role", "primary"),
                holes=[holes], open_spots=spots,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                booking_url=course.get("booking_url", f"https://{host}"),
            ))
        return out
