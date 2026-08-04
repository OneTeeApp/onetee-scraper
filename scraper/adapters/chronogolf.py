"""Chronogolf (Lightspeed Golf) adapter — fully API-driven, no browser needed.

Discovery chain (all plain HTTP, verified July 2026 via live capture):

  1. GET /private_api/clubs/<slug or club_id>
       -> { id, uuid, settings.default_affiliation_type_id }
  2. GET /private_api/clubs/<club_id>/courses
       -> [ { id, name, online_booking_enabled }, ... ]
  3. GET /marketplace/clubs/<club_id>/teetimes
         ?date=YYYY-MM-DD
         &course_id=<course_id>
         &affiliation_type_ids[]=<aff>&affiliation_type_ids[]=<aff>   (one per player)
         &nb_holes=18
       -> [ { start_time, date, hole, out_of_capacity,
              green_fees:[{ green_fee, affiliation_type_id, ... }] }, ... ]

The affiliation_type_id ("Public" green-fee category) is the crux — it lives at
settings.default_affiliation_type_id and MUST be supplied or the API 422s with
"Player type provided is not valid". We send it twice (2-player pricing) to get
a representative rate; open capacity is read from out_of_capacity.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading
from typing import Any

from .base import Adapter, PartialFetchError
from ..models import TeeTime

BASE = "https://www.chronogolf.com"

# discover() resolves club_id / affiliation_type_id / course_ids — all STABLE
# per course — but it ran two /private_api calls on EVERY fetch (per course, per
# date, per pass). At the near tier's cadence that is a lot of load on
# chronogolf's API, and when discovery flaked the whole course errored and its
# near-date rows went stale and hid (measured 2026-08-04: city-park-nine /
# south-suburban near flicker). Cache the first successful discovery (memory +
# runner disk) and reuse it for the rest of the run — the per-date teetimes call
# still runs live, but the two discovery calls collapse to one per course/run.
_DISC_CACHE_PATH = pathlib.Path(".cache/chronogolf_discovery.json")
_DISC_MEM: dict[str, dict] = {}
_DISC_LOADED = [False]
_DISC_LOCK = threading.Lock()


def _disc_cache_get(key: str) -> dict | None:
    with _DISC_LOCK:
        if not _DISC_LOADED[0]:
            try:
                _DISC_MEM.update(json.loads(_DISC_CACHE_PATH.read_text()))
            except Exception:  # noqa: BLE001 — cold cache is fine
                pass
            _DISC_LOADED[0] = True
        return _DISC_MEM.get(str(key))


def _disc_cache_put(key: str, disc: dict) -> None:
    with _DISC_LOCK:
        _DISC_MEM[str(key)] = disc
        try:
            _DISC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DISC_CACHE_PATH.write_text(json.dumps(_DISC_MEM))
        except Exception:  # noqa: BLE001 — best-effort persistence
            pass


class ChronogolfAdapter(Adapter):
    platform = "chronogolf"

    # -- discovery -----------------------------------------------------------

    def _club(self, slug_or_id: str) -> dict:
        return self.get_json(f"{BASE}/private_api/clubs/{slug_or_id}")

    def _courses(self, club_id: int) -> list[dict]:
        data = self.get_json(f"{BASE}/private_api/clubs/{club_id}/courses")
        return data if isinstance(data, list) else data.get("courses", [])

    def discover(self, slug_or_id: str) -> dict:
        """Resolve everything the fetch needs from a slug or numeric club id.

        Cache-first (memory + runner disk): the result is stable per course, so
        after the first successful discovery in a run the two /private_api calls
        are skipped entirely for the rest of the run.
        """
        cached = _disc_cache_get(slug_or_id)
        if cached and cached.get("club_id") is not None:
            cn = cached.get("course_names") or {}
            # JSON turns the int course-id keys into strings; restore them so
            # the fetch's `course_names.get(cid)` (cid is an int) still hits.
            fixed = {}
            for k, v in cn.items():
                try:
                    fixed[int(k)] = v
                except (TypeError, ValueError):
                    fixed[k] = v
            return {**cached, "course_names": fixed}
        club = self._club(slug_or_id)
        club_id = club["id"]
        aff = (club.get("settings") or {}).get("default_affiliation_type_id")
        courses = [c for c in self._courses(club_id)
                   if c.get("online_booking_enabled")]
        disc = {"club_id": club_id, "affiliation_type_id": aff,
                # CLUB-level flag: an unclaimed Chronogolf *directory listing*
                # has online_booking_enabled=False (and no seller) even though
                # its course rows can still show online_booking_enabled=True.
                # Those clubs never sell tee times through Chronogolf, so the
                # marketplace API always returns 0 — guard on this to skip them
                # instead of burning a request per course every scrape.
                "club_bookable": bool(club.get("online_booking_enabled")),
                "course_ids": [c["id"] for c in courses],
                "course_names": {c["id"]: c.get("name", "") for c in courses}}
        _disc_cache_put(slug_or_id, disc)
        return disc

    # -- fetch ---------------------------------------------------------------

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        ids = course["ids"]
        key = ids.get("club_id") or ids.get("slug")
        if not key:
            raise ValueError(f"{course['slug']}: no chronogolf slug/club_id")

        disc = self.discover(str(key))
        if not disc["club_bookable"]:
            raise RuntimeError(
                f"{course['slug']}: unclaimed Chronogolf directory listing "
                "(club online_booking disabled) — books through another engine")
        aff = disc["affiliation_type_id"]
        if not aff:
            raise RuntimeError(f"{course['slug']}: no default_affiliation_type_id "
                               "(club may be contact-only / no online booking)")
        course_ids = ids.get("course_ids") or disc["course_ids"]
        if not course_ids:
            raise RuntimeError(f"{course['slug']}: no online-bookable courses")

        multi = len(course_ids) > 1
        out: list[TeeTime] = []
        failed_cids: list[tuple] = []       # (cid, first error)
        for cid in course_ids:
            # The API has no remaining-spots field — out_of_capacity is relative
            # to the REQUESTED party size (probe-verified). So ask at party
            # sizes 4 -> 1: a slot's true remaining seats = the largest party
            # that still fits. Replaces the old hardcoded "4" guess.
            by_slot: dict = {}
            first_err: Exception | None = None
            failed_sizes = 0
            for n in (4, 3, 2, 1):
                params = [("date", date.isoformat()), ("course_id", cid)]
                params += [("affiliation_type_ids[]", aff)] * n
                params.append(("nb_holes", 18))
                try:
                    slots = self.get_json(
                        f"{BASE}/marketplace/clubs/{disc['club_id']}/teetimes",
                        params=params)
                except Exception as exc:  # noqa: BLE001 — one size failing
                    slots = []            # shouldn't kill the whole course
                    failed_sizes += 1
                    first_err = first_err or exc
                for slot in slots or []:
                    if slot.get("out_of_capacity"):
                        continue
                    sid = slot.get("id") or slot.get("uuid") or slot.get("start_time")
                    e = by_slot.setdefault(sid, {"slot": slot, "spots": n})
                    e["spots"] = max(e["spots"], n)

            # ...but ALL FOUR sizes failing is not "empty", it is "unknown".
            # The blanket swallow used to turn a teetimes endpoint that was
            # down (while discovery still answered) into a clean [] — the
            # course landed in courses_empty and sync deactivated its whole
            # day. Unknown must raise (below, after the sibling cids get
            # their chance) so the error guard shields the rows.
            if failed_sizes == 4:
                failed_cids.append((cid, first_err))
                continue

            cname = disc["course_names"].get(cid, course["name"])
            for e in by_slot.values():
                slot = e["slot"]
                fees = [f.get("green_fee") for f in slot.get("green_fees", [])
                        if isinstance(f.get("green_fee"), (int, float))]
                start = slot.get("start_time", "")
                if not start:
                    continue    # no start time -> a malformed "...T:00" row
                out.append(self.base_tee_time(
                    course,
                    teetime=f"{slot.get('date', date.isoformat())}T{start}"
                            + ("" if len(start) > 5 else ":00"),
                    course_label=cname if multi else "",
                    holes=[18],
                    open_spots=e["spots"],
                    price_min=min(fees) if fees else None,
                    price_max=max(fees) if fees else None,
                    raw={"course_name": cname, **{k: slot.get(k) for k in
                         ("start_time", "hole", "out_of_capacity")}},
                ))

        if failed_cids and len(failed_cids) == len(course_ids):
            raise RuntimeError(
                f"{course['slug']}: teetimes endpoint failed at every party "
                f"size for every course_id") from failed_cids[0][1]
        if failed_cids:
            # Partial: publish the sibling courses that served; the failed
            # cids' labels shield their existing rows from deactivation.
            raise PartialFetchError(
                f"{course['slug']}: teetimes endpoint failed for course_id(s) "
                + ", ".join(str(c) for c, _ in failed_cids),
                tee_times=out,
                failed_labels=[disc["course_names"].get(c, course["name"])
                               if multi else "" for c, _ in failed_cids])
        return out
