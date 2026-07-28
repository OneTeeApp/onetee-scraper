"""ClubEssential / NetCaddy adapter — plain JSON, no auth (captured July 2026).

The public "Book a Tee Time" widget on a ClubEssential club site is a thin
shell around one REST call on the club's own host:

  GET https://<host>/a_master/net/netcaddy/api/teetimes/Available
        ?courseId=GOLFCOURSE-<id>[,GOLFCOURSE-<id>...]
        &date=M/D/YYYY&enddate=M/D/YYYY
        &st=12:00AM&ed=11:59PM
        &min=1&max=4&rCode=&pCode=&gCode=

  -> { "ServerTimeUTC": "...",
       "Results": [ { "SiteID": 2060, "CourseId": 84, "holes": 18,
                      "startTime": "2026-07-31T09:20:00",     <- club-local
                      "status": 0,
                      "Golfers": [ { "MemberId": null, "FirstName": null,
                                     "RoundFeeAmount": 78.99, ... }, ... ] },
                    ... ] }

Only *available* slots come back, and `Golfers` is one entry per still-open
seat — so len(Golfers) is the open-spot count and Golfers[0].RoundFeeAmount
is the per-player rate. `st`/`ed` narrow the window; a full-day window
returns everything, so we always ask for the full day and let the sheet
decide (measured: 12:00AM-11:59PM and 04:00AM-09:00PM both returned the
same 44 rows for 2026-07-31).

WHY courseId IS PINNED, NEVER GUESSED

`courseId` is resolved per host, and a host can front several clubs — the
McConnell Golf site serves more than a dozen. A control probe with
GOLFCOURSE-1 returned zero rows rather than a stranger's sheet, which is
better than teesnap behaves, but "usually fails closed" is not a guarantee
worth publishing another club's tee sheet on. So the ids come from
EXTRA_IDS, read out of the club's own widget request, and every row is
checked against the pinned SiteID before it is emitted. A row from an
unexpected site is dropped, loudly, rather than relabelled as ours.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime

PATH = "/a_master/net/netcaddy/api/teetimes/Available"


class ClubEssentialAdapter(Adapter):
    platform = "clubessential"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        host = ids.get("host")
        course_ids = ids.get("course_ids") or []
        if not host or not course_ids:
            raise ValueError(f"{course['slug']}: clubessential needs host + "
                             "course_ids (pin them in EXTRA_IDS)")
        site_id = ids.get("site_id")

        params = {
            "courseId": ",".join(f"GOLFCOURSE-{c}" for c in course_ids),
            "date": f"{date.month}/{date.day}/{date.year}",
            "enddate": f"{date.month}/{date.day}/{date.year}",
            "st": "12:00AM", "ed": "11:59PM",
            "min": 1, "max": 4,
            "rCode": "", "pCode": "", "gCode": "",
        }
        data = self.get_json(f"https://{host}{PATH}", params=params,
                             headers={"Accept": "application/json"})
        results = (data or {}).get("Results") or []

        wanted = {int(c) for c in course_ids}
        labels = ids.get("course_labels") or {}
        multi = len(wanted) > 1
        booking_url = course.get("booking_url", f"https://{host}")

        out: list[TeeTime] = []
        foreign = 0
        for row in results:
            if site_id is not None and row.get("SiteID") != site_id:
                foreign += 1        # another club on this host — never ours
                continue
            try:
                cid = int(row.get("CourseId"))
            except (TypeError, ValueError):
                continue
            if cid not in wanted:
                foreign += 1
                continue
            start = row.get("startTime")
            if not isinstance(start, str) or not start.startswith(date.isoformat()):
                continue            # the sheet answered for another day

            golfers = row.get("Golfers") or []
            spots = len(golfers) or None
            fees = [g.get("RoundFeeAmount") for g in golfers
                    if isinstance(g.get("RoundFeeAmount"), (int, float))]
            holes_val = row.get("holes")
            holes = [int(holes_val)] if isinstance(holes_val, int) else [18]

            out.append(TeeTime(
                course_slug=course["slug"], course_name=course["name"],
                city=course.get("city", ""), platform=self.platform,
                teetime=start[:19],
                course_label=str(labels.get(str(cid), "")) if multi else "",
                state=course.get("state", ""),
                venue_id=course.get("venue_id", course["slug"]),
                source_role=course.get("source_role", "primary"),
                holes=holes, open_spots=spots,
                price_min=min(fees) if fees else None,
                price_max=max(fees) if fees else None,
                booking_url=booking_url,
            ))

        if foreign and not out:
            # Everything the host returned belonged to somebody else. Say so
            # instead of reporting a quiet zero that reads like "closed today".
            raise RuntimeError(
                f"{course['slug']}: netcaddy returned {foreign} row(s), none "
                f"matching site {site_id} / courses {sorted(wanted)}")
        return out
