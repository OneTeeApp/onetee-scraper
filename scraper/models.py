"""Unified data model for aggregated tee times."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class TeeTime:
    """One bookable tee time slot, normalized across booking platforms."""

    course_slug: str            # our registry slug
    course_name: str
    city: str
    platform: str               # foreup | teeitup | chronogolf | clubprophet | ...
    teetime: str                # ISO 8601 local course time, e.g. "2026-07-24T07:30:00"
    holes: list[int] = field(default_factory=list)   # e.g. [9, 18]
    open_spots: Optional[int] = None                 # players that can still book
    price_min: Optional[float] = None                # USD
    price_max: Optional[float] = None                # USD
    currency: str = "USD"
    booking_url: str = ""       # deep link a user can book at
    simulated: bool = False     # True if generated sample data, not live
    raw: dict[str, Any] = field(default_factory=dict)  # platform-native payload

    def to_dict(self, include_raw: bool = False) -> dict:
        d = asdict(self)
        if not include_raw:
            d.pop("raw", None)
        return d


@dataclass
class FetchResult:
    """Outcome of one course fetch."""

    course_slug: str
    platform: str
    ok: bool
    tee_times: list[TeeTime] = field(default_factory=list)
    error: Optional[str] = None
