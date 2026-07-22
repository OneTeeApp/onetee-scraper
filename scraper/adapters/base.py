"""Adapter base class + shared HTTP session."""
from __future__ import annotations

import abc
import datetime as dt
import logging
from typing import Any

import requests

from ..models import TeeTime

log = logging.getLogger("teetime")

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
                 params: dict | None = None) -> Any:
        r = self.session.get(url, headers=headers, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

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
