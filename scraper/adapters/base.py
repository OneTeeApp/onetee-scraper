"""Adapter base class + shared HTTP session."""
from __future__ import annotations

import abc
import datetime as dt
import logging
import random
import time
from typing import Any

import requests

from ..models import TeeTime

log = logging.getLogger("teetime")

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2  # keep total run time well under the 15-min workflow cap

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 20


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


class Adapter(abc.ABC):
    """One adapter per booking platform.

    Subclasses implement fetch() -> list[TeeTime] for a single course+date.
    Adapters must raise on hard failures; the aggregator catches and records.
    """

    platform: str = "base"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or make_session()

    @abc.abstractmethod
    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        """Fetch tee times for `course` (a registry entry) on `date`."""

    # -- helpers -------------------------------------------------------------

    def get_json(self, url: str, *, headers: dict | None = None,
                 params: Any = None) -> Any:
        """GET with polite retry/backoff on rate limits and 5xx."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get(url, headers=headers, params=params,
                                     timeout=TIMEOUT)
                if r.status_code in RETRY_STATUS:
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                r.raise_for_status()
                return r.json()
            except (requests.HTTPError, requests.ConnectionError,
                    requests.Timeout) as e:
                last_exc = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and status not in RETRY_STATUS:
                    raise
                if attempt < MAX_RETRIES - 1:
                    # short backoff w/ jitter; respect Retry-After but cap it so
                    # one slow host can't blow the workflow time budget
                    ra = getattr(getattr(e, "response", None), "headers", {})
                    wait = min(float(ra.get("Retry-After", 0)), 5) if ra else 0
                    time.sleep(max(wait, (1.5 ** attempt) + random.uniform(0, 0.5)))
        raise last_exc  # exhausted retries

    @staticmethod
    def base_tee_time(course: dict[str, Any], **kw) -> TeeTime:
        return TeeTime(
            course_slug=course["slug"],
            course_name=course["name"],
            city=course.get("city", ""),
            platform=course["platform"],
            booking_url=course.get("booking_url", ""),
            **kw,
        )
