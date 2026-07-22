"""Club Caddie adapter — pending live capture.

Booking SPA at apimanager-cc<NN>.clubcaddie.com/webapi/view/<token> (and
customer-cc<NN>.clubcaddie.com/customer-login?clubid=<id>). The tee-sheet JSON
endpoint is served over XHR and needs one devtools capture per shard to pin the
exact path + params. Nine CO courses use it. Raises clearly until captured so
coverage reporting stays honest.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .base import Adapter
from ..models import TeeTime


class ClubCaddieAdapter(Adapter):
    platform = "clubcaddie"

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        # Captured July 2026: /webapi/view/<token>/slots returns JSON only through
        # a stateful session handshake (SetSessionIdInLocalStorage bootstrap +
        # single-use Interaction id); plain replays get the HTML shell and the
        # session-init call 503s. This is anti-automation behaviour like GolfNow.
        # Route for these courses is the Club Caddie partner API, not scraping.
        raise RuntimeError(
            "Club Caddie tee sheet is behind a stateful anti-automation session; "
            "use the Club Caddie partner API. Booking URL: "
            + course.get("booking_url", ""))
