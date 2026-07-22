"""Club Prophet (CPS Golf, *.cps.golf) — requires OAuth, not scrapable anonymously.

Captured July 2026: the tee-time API
(/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes) requires a Bearer JWT
issued by the tenant's OpenID identity server (/identityapi/connect/token) to a
confidential client ("onlinereswebshortlived") via the client_credentials grant.
Minting that token needs a client secret embedded in the site's JS bundle —
i.e. harvesting a credential — which we will not do.

The correct route for CPS courses is the Club Prophet partner/API program
(commercial agreement), not anonymous scraping. Left as a clear no-op so
coverage reporting is honest. 12 CO courses use CPS.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime


class ClubProphetAdapter(Adapter):
    platform = "clubprophet"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        raise RuntimeError(
            "Club Prophet (cps.golf) API is OAuth-protected; requires the CPS "
            "partner program, not anonymous scraping. Booking URL: "
            + course.get("booking_url", ""))
