"""GolfBack adapter — one anonymous POST per course-day, no auth.

Captured live 2026-07-30 from the booking SPA at golfback.com by patching
window.fetch/XHR and letting the app request its own tee sheet.

  GET  https://api.golfback.com/api/v1/courses/<uuid>
       -> { "data": { id, companyId, name, city, state, zipCode,
                      isMultiCourse, multiCourseIds: [...], ... } }

  POST https://api.golfback.com/api/v1/courses/<uuid>/date/YYYY-MM-DD/teetimes
       Content-Type: application/json
       body: {"sessionId": null}
       -> { "data": [ { id, courseId, courseName,
                        dateTime:      "2026-08-01T10:30:00+00:00",
                        localDateTime: "2026-08-01T06:30:00",
                        rates: [ { name, description, holes, hasCartIncluded,
                                   isPrimary, basePrice, price, ... } ],
                        primaryPrices: [ { holes, basePrice, price } ],
                        isAvailable, holes: [18, 9], has9Holes,
                        playersMin, playersMax, playersDisplay }, ... ] }

It must be a POST. A GET on the same path answers 405, so a reader who
assumes REST conventions concludes the endpoint is gone. The body is the
literal {"sessionId": null} the SPA sends; no token, cookie or referer check
was needed on any course tried.

THE ONE TRAP: `dateTime` IS UTC LABELLED "+00:00" ON A LOCAL-LOOKING VALUE.
For a 06:30 Eastern slot it reads "2026-08-01T10:30:00+00:00" — the instant is
right, the offset string is a lie about what the club sees. Parse that field and
every tee time shifts by the UTC offset, silently, in the direction that makes
mornings look like mid-day. `localDateTime` is the club's own wall clock and is
the only field this adapter reads. Measured on four courses in two states.

Booking windows vary a lot and that is normal: Challedon, Orange Lake Legends
and Palm Beach Par 3 all went empty past ~2 weeks while Mission Resort's El
Campeon still served 62 rows at +30 days and 5 at +45. A far-tier zero on a
short-window course is honest, not a fault.

`playersMax` is the real open-spot count and varies per slot (1-4 measured on a
single sheet), so it is published as open_spots rather than assumed to be 4.

Every course we carry has its own uuid in its booking URL, so nothing needs
pinning. `isMultiCourse`/`multiCourseIds` exist in the schema though, and four
Orange Lake courses share one company, so each row's `courseId` is still checked
against the uuid we asked for: a sheet that comes back belonging to a sibling is
dropped and counted, never relabelled as ours.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

API = "https://api.golfback.com/api/v1/courses"


class GolfBackAdapter(Adapter):
    platform = "golfback"

    @staticmethod
    def _prices(row: dict[str, Any]) -> list[float]:
        """Every positive price the slot advertises, cart included.

        `price` is what the booking card charges (cart included per the rate's
        own description); `basePrice` is the pre-fee number and is sometimes
        lower for the same rate (Challedon: basePrice 81, price 102). Both are
        collected so the published range brackets what a golfer can actually
        pay, and non-positive values are dropped rather than published: a
        `primaryPrices` entry for a hole count the course is not selling that
        day carries `price: null` (measured on Challedon's 9-hole entry), and
        publishing that as $0 would advertise a free round.
        """
        out: list[float] = []
        for src in (row.get("rates") or []) + (row.get("primaryPrices") or []):
            if not isinstance(src, dict):
                continue
            for key in ("price", "basePrice"):
                v = src.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    out.append(float(v))
        return out

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        uuid = course["ids"].get("course_uuid")
        if not uuid:
            raise ValueError(f"{course['slug']}: golfback needs course_uuid "
                             "(the uuid in the booking URL)")
        data = self.post_json(
            f"{API}/{uuid}/date/{date.isoformat()}/teetimes",
            json={"sessionId": None},
            headers={"Accept": "application/json",
                     # Sent by the real client. Nothing rejected a request
                     # without them, but a widget API is free to start caring.
                     "Origin": "https://golfback.com",
                     "Referer": "https://golfback.com/"},
        )
        rows = (data or {}).get("data") or []
        if not isinstance(rows, list):
            raise RuntimeError(f"{course['slug']}: golfback returned "
                               f"{type(rows).__name__}, expected a list")

        # Keep only what is ours and for the right day, THEN decide labelling.
        keep: list[dict] = []
        foreign = wrong_day = 0
        for row in rows:
            if not isinstance(row, dict) or row.get("isAvailable") is False:
                continue
            if row.get("courseId") and row["courseId"] != uuid:
                foreign += 1
                continue
            local = row.get("localDateTime")
            if not isinstance(local, str) or not local.startswith(date.isoformat()):
                wrong_day += 1      # the sheet answered for another day
                continue
            keep.append(row)

        # A venue whose own sheet spans sub-courses should label them; a
        # single-course venue must not, or the label joins the D1 primary key
        # for no reason. Decide from the rows we KEPT, never from everything on
        # the wire: judging by the raw response let one leaked sibling row
        # switch labelling on for a single-course venue, which is how a course
        # silently acquires a course_label it has never had before.
        multi = len({r.get("courseName") for r in keep if r.get("courseName")}) > 1

        out: list[TeeTime] = []
        for row in keep:
            local = row["localDateTime"]
            holes = sorted({int(h) for h in (row.get("holes") or [])
                            if isinstance(h, int) and h > 0}) or [18]
            prices = self._prices(row)
            spots = row.get("playersMax")
            out.append(self.base_tee_time(
                course,
                teetime=local[:19],
                course_label=str(row.get("courseName") or "") if multi else "",
                holes=holes,
                open_spots=int(spots) if isinstance(spots, int) and spots > 0 else None,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
            ))

        if not out and (foreign or wrong_day):
            # Everything on the wire belonged to somebody else or to another
            # date. Raise instead of reporting the zero that sync() would act
            # on by deactivating this course's day.
            raise RuntimeError(
                f"{course['slug']}: golfback returned {len(rows)} row(s), none "
                f"usable ({foreign} for another courseId, {wrong_day} for "
                f"another day) — check the pinned uuid {uuid}")
        return out
