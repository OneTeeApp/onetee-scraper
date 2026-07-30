"""GolfNow TeeItUp adapter.

Booking pages: https://<alias>.book.teeitup.com/ (also .book.teeitup.golf,
<alias>.play.teeitup.com). All are the same SPA backed by a public JSON API:

    GET https://phx-api-be-east-1b.kenna.io/v2/tee-times?date=YYYY-MM-DD
        header: x-be-alias: <alias>

Optional params: facilityIds=<id>, and the same API serves course/facility
metadata at /v2/courses (with the alias header), which is how we discover
facility ids from just the alias.

Response shape (MEASURED, probe-results/diag_kenna_slots.txt) — a JSON array
with one object per facility/day:
    [{"dayInfo": {...}, "teetimes": [
        {"teetime": "2026-07-24T13:30:00.000Z",
         "courseId": "54f14b510c8ad60378b00df6",   # Mongo id, NOT the integer
         "maxPlayers": 4, "minPlayers": 1, "backNine": false,
         "rates": [{"greenFeeWalking": 6500, "greenFeeCart": 8500,
                    "holes": 18, "name": "...",
                    "golfnow": {"GolfFacilityId": 287, ...}}], ...}]}]
Prices are in cents.

TWO IDENTITIES, AND ONLY ONE OF THEM IS IN A SLOT. A facility row carries an
integer `id` (287) AND a Mongo `courseId` ("54f14b510c8ad60378b00df6"); the
registry pins the integer, because that is what `facilityIds` takes. A SLOT
carries only the Mongo `courseId` — there is no top-level `facilityId` key at
all (the probe counted `facilityId` as None on 304/304, 51/51 and 51/51
slots). Any client-side filter that compares the pinned integer against a
slot therefore matches NOTHING and deletes the whole sheet; that is exactly
what happened between a248c79 and 41794cd, measured as before=4487 after=0
across ten pinned courses. The integer does survive, but only nested at
rates[].golfnow.GolfFacilityId. Map pin -> courseId through the facilities
list before comparing anything.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import time as _time
import zoneinfo
from typing import Any

from .base import Adapter
from ..models import TeeTime

API_BASE = "https://phx-api-be-east-1b.kenna.io"

# kenna's `teetime` strings are true UTC ("2026-07-25T18:40:00.000Z" = 12:40 PM
# Denver — probe-verified). They MUST be converted to course-local time or the
# site shows times ~6-7h in the future (which also read as bookable-past slots).
# Facility metadata carries the IANA timeZone and is what we use whenever it is
# available; this table is the fallback for when the lookup fails.
#
# It listed CO, AZ and VA only, so Maryland and Florida — 175 live TeeItUp
# courses between them — fell through `.get(state, "America/Denver")` to
# MOUNTAIN time. That is two hours early on an Eastern sheet, in the direction
# that turns a 6:30am slot into 4:30am and pushes the day's real tee times
# behind the API's past-cutoff. It has not fired in D1 yet (checked
# 2026-07-30: zero MD/FL rows before 06:00), but it is one rate-limited
# facilities call away, and the run that measured it was already taking 123 of
# those per shard.
#
# FLORIDA IS NOT ONE TIMEZONE. The panhandle west of the Apalachicola River —
# Pensacola, Destin, Freeport (Windswept Dunes is ours) — is Central. Eastern
# is the right default for the state because that is where all but a handful of
# Florida courses are, but it is a default over a genuinely split state, which
# is exactly why the facility's own timeZone must stay the primary source and
# this must stay the fallback.
_STATE_TZ = {"CO": "America/Denver", "AZ": "America/Phoenix",
             "VA": "America/New_York", "MD": "America/New_York",
             "FL": "America/New_York"}
_TZ_DEFAULT = "America/New_York"

# ---------------------------------------------------------------------------
# Facility metadata cache, on disk, shared by every process on this runner.
#
# A facility's integer id, Mongo courseId, name and timeZone are its identity,
# not its inventory: they do not change between today and next Tuesday. They
# were nonetheless re-fetched from scratch for EVERY DATE, because each date is
# its own `python -m scraper.aggregate` process and `_FACILITIES` lives in that
# process's memory. 330 of the fleet's 353 TeeItUp courses have their own
# alias, so almost none of that work is shared: ~82 redundant calls per shard
# per date, against the single host that rate-limits the whole fleet.
#
# The near tier makes it much worse than that sounds. It loops three dates
# every five minutes for five and a half hours — roughly 66 passes, each
# spawning three fresh processes — so one shard re-asks kenna for the same
# unchanged facility list on the order of 16,000 times per run.
#
# Measured 2026-07-30 in mid-tier run #213: 123 HTTP 429s in shard 0 alone,
# errors climbing 69 -> 117 between the first date and the second while
# captured tee times fell 6455 -> 3582. The throttle was never the binding
# constraint; the redundancy was.
#
# This changes nothing about what is fetched, how it is paced, or what is
# published. It only stops asking a second time for an answer already on disk.
# ---------------------------------------------------------------------------
_CACHE_DEFAULT = ".cache/kenna_facilities.json"
# Read per call, not once at import, so a test or a diag can turn it off after
# the module is loaded. Set KENNA_FACILITIES_CACHE="" to force every lookup to
# go to kenna — which is what you want when the question is "what does kenna
# say right now", and never what you want in a scrape.
_CACHE_DISABLED = {"", "0", "off", "none"}


def _cache_path() -> str:
    p = os.environ.get("KENNA_FACILITIES_CACHE", _CACHE_DEFAULT)
    return "" if p.strip().lower() in _CACHE_DISABLED else p
# Long enough to cover a 5.5-hour near-tier run and every hourly tier in
# between; short enough that a club renaming a course or moving timezone heals
# by itself within a week without anyone remembering this file exists.
_CACHE_TTL = dt.timedelta(days=7)
_DISK: dict[str, dict] | None = None
_DISK_LOCK = threading.Lock()


def _disk_reset() -> None:
    """Drop the in-memory view of the file. For tests; harmless in a scrape."""
    global _DISK
    with _DISK_LOCK:
        _DISK = None


def _disk_load(path: str) -> dict[str, dict]:
    global _DISK
    if _DISK is None:
        try:
            with open(path) as fh:
                blob = json.load(fh)
            _DISK = blob.get("aliases", {}) if isinstance(blob, dict) else {}
        except (OSError, ValueError):
            _DISK = {}                 # absent or corrupt: start clean, no fuss
    return _DISK


def _disk_get(alias: str) -> list | None:
    path = _cache_path()
    if not path:
        return None
    with _DISK_LOCK:
        entry = _disk_load(path).get(alias)
    if not isinstance(entry, dict):
        return None
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
            entry["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if age > _CACHE_TTL:
        return None
    facilities = entry.get("facilities")
    return facilities if isinstance(facilities, list) and facilities else None


def _disk_put(alias: str, facilities: list) -> None:
    """Persist one alias. Empty lists are NOT stored.

    A facilities list that came back empty is indistinguishable here from one
    that came back empty because kenna was shedding load, and writing it down
    would turn a transient throttle into a week of "this alias has no
    courses" — the same shape of bug as the silent-empty fallback this file
    already carries a long comment about. Re-asking on the rare genuinely
    empty alias is cheap; remembering a wrong answer is not.
    """
    if not facilities:
        return
    path = _cache_path()
    if not path:
        return
    with _DISK_LOCK:
        cache = _disk_load(path)
        cache[alias] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(
                            timespec="seconds"),
                        "facilities": facilities}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            # Written through a temp file in the same directory and renamed, so
            # a run cancelled mid-write (which is how every mid-tier run ended
            # before the timeout was raised) leaves the previous cache intact
            # rather than a truncated file every later process has to discard.
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                       suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump({"v": 1, "aliases": cache}, fh)
            os.replace(tmp, path)
        except OSError:
            pass                       # a read-only workspace must not fail a scrape


# All TeeItUp courses hit one shared kenna host, so the WHOLE fleet — across
# every parallel shard — must stay under its burst 429 limit. Within a process
# we cap concurrency and space requests; across S shards we widen the per-shard
# gap to GAP*S so the aggregate cadence is constant no matter how many shards
# run (SHARD_COUNT is published by scraper.sharding). This is what lets TeeItUp
# scale to thousands of courses without one shard's pace multiplying by S.
_KENNA_SEM = threading.Semaphore(2)      # <=2 concurrent kenna.io reqs per shard
_KENNA_BASE_GAP = 0.7                     # global min seconds between requests
_KENNA_LOCK = threading.Lock()
_KENNA_LAST = [0.0]


def _kenna_gap() -> float:
    try:
        shards = max(1, int(os.environ.get("SHARD_COUNT", "1")))
    except ValueError:
        shards = 1
    return _KENNA_BASE_GAP * shards


def _kenna_throttle():
    with _KENNA_LOCK:
        wait = _kenna_gap() - (_time.monotonic() - _KENNA_LAST[0])
        if wait > 0:
            _time.sleep(wait)
        _KENNA_LAST[0] = _time.monotonic()


class TeeItUpAdapter(Adapter):
    platform = "teeitup"

    # alias -> {courseId: {"name": str, "tz": str}} — one kenna call per alias
    # per process, shared across threads.
    _META: dict[str, dict] = {}
    _META_LOCK = threading.Lock()
    # alias -> raw /v2/courses payload, so the ids and the labels come from ONE
    # call. kenna 429s the whole fleet off one host, so every saved request
    # counts (probe-results/verify_fixes.txt section A shows live 429s).
    _FACILITIES: dict[str, list] = {}

    def _headers(self, alias: str) -> dict:
        return {"x-be-alias": alias}

    def _facilities_cached(self, alias: str) -> list[dict]:
        """discover_facilities() with a per-alias cache. Raises on failure.

        Three layers, cheapest first: this process's memory, then the runner's
        disk (see the cache notes at the top of this file), then kenna.
        """
        with self._META_LOCK:
            if alias in self._FACILITIES:
                return self._FACILITIES[alias]
        stored = _disk_get(alias)
        if stored is not None:
            with self._META_LOCK:
                self._FACILITIES[alias] = stored
            return stored
        with _KENNA_SEM:
            _kenna_throttle()
            facilities = self.discover_facilities(alias)
        facilities = facilities if isinstance(facilities, list) else []
        with self._META_LOCK:
            self._FACILITIES[alias] = facilities
        _disk_put(alias, facilities)
        return facilities

    def _facility_meta(self, alias: str) -> dict:
        """courseId -> {name, tz} for an alias (cached; empty dict on failure)."""
        with self._META_LOCK:
            if alias in self._META:
                return self._META[alias]
        meta: dict = {}
        try:
            for f in self._facilities_cached(alias):
                cid = f.get("courseId") or f.get("id")
                if cid is not None:
                    meta[str(cid)] = {"name": f.get("name") or "",
                                      "tz": f.get("timeZone") or ""}
        except Exception:  # noqa: BLE001 — fall back to state tz, no labels
            pass
        with self._META_LOCK:
            self._META[alias] = meta
        return meta

    @staticmethod
    def _to_local(utc_iso: str, tz_name: str) -> str:
        """'2026-07-25T18:40:00.000Z' + America/Denver -> '2026-07-25T12:40:00'."""
        try:
            t = dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
            if t.tzinfo is None:
                return utc_iso                    # already naive/local: keep
            local = t.astimezone(zoneinfo.ZoneInfo(tz_name))
            return local.replace(tzinfo=None).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001 — malformed input: keep raw
            return utc_iso

    def discover_facilities(self, alias: str) -> list[dict]:
        """Return facility metadata (ids, names) for an alias.

        /alias/<alias>/facilities is tried FIRST. It used to be the fallback,
        on the belief that most aliases answer /v2/courses and only a few
        (Granby Ranch) needed the older route. That is no longer true, if it
        ever was: probe-results/diag_kenna_routes.txt section 2 put twelve
        live registry aliases through both routes and /v2/courses answered
        0/12 while /alias/<alias>/facilities answered 12/12. Leading with the
        dead route cost one wasted request per alias against the host that
        429s the whole fleet.

        /v2/courses is kept as the fallback rather than deleted — it is the
        newer route and may come back — but a 404 there is now the expected
        case, not the exception.
        """
        try:
            data = self.get_json(f"{API_BASE}/alias/{alias}/facilities",
                                 headers=self._headers(alias))
        except Exception:  # noqa: BLE001 — try the newer route before failing
            data = self.get_json(f"{API_BASE}/v2/courses",
                                 headers=self._headers(alias))
        return data if isinstance(data, list) else data.get("courses", [])

    def _teetimes(self, alias: str, date: dt.date, facility_ids=None):
        params: dict[str, Any] = {"date": date.isoformat()}
        if facility_ids:
            params["facilityIds"] = facility_ids
        with _KENNA_SEM:
            _kenna_throttle()
            return self.get_json(f"{API_BASE}/v2/tee-times",
                                 headers=self._headers(alias), params=params)

    def _pinned_course_ids(self, alias: str, facility_id) -> set[str]:
        """Pinned integer facility id(s) -> the Mongo courseIds slots carry.

        The registry pins `facilityIds` values (287) because that is what the
        query parameter takes, but a slot names its course with a Mongo id.
        Empty set means the mapping is unavailable (facilities lookup failed,
        or the pin is not in this alias's list) — callers must NOT read that
        as "nothing matches".
        """
        pins = {p.strip() for p in str(facility_id).split(",") if p.strip()}
        out: set[str] = set()
        if not pins:
            return out
        try:
            for f in self._facilities_cached(alias):
                if isinstance(f, dict) and str(f.get("id")) in pins:
                    if f.get("courseId"):
                        out.add(str(f["courseId"]))
        except Exception:  # noqa: BLE001 — fall through to the nested-id path
            pass
        return out

    def _facility_ids(self, alias: str) -> str:
        """Resolve this alias's facility/course ids via /v2/courses."""
        ids = []
        for c in self._facilities_cached(alias):
            fid = c.get("id") or c.get("facilityId") or c.get("courseId")
            if fid:
                ids.append(str(fid))
        return ",".join(ids)

    def fetch(self, course: dict[str, Any], date: dt.date) -> list[TeeTime]:
        alias = course["ids"].get("alias")
        if not alias:
            raise ValueError(f"{course['slug']}: missing TeeItUp alias "
                             "(booking URL did not yield one)")
        facility_id = course["ids"].get("facility_id")

        # facilityIds is REQUIRED, not optional. Measured live on 10 pinned
        # courses x 3 dates (probe-results/verify_fixes.txt section A): the
        # call WITH the pinned id returns the real sheet — aguila 56/61/61,
        # cave-creek 54/73/68, encanto-18 63/87/83, las-sendas 51/62/62,
        # ak-chin 41/0/47 — while the bare per-alias call on the same alias
        # and date returns a dayInfo with an EMPTY teetimes list every time.
        # 869 slots vs 0.
        #
        # a248c79 removed the param on the strength of one diag4 sample where
        # every alias 500'd. That sample was transient: kenna's gateway
        # intermittently 5xxs and 429s (this run caught live 429s too), which
        # is exactly why get_json already retries 5xx. One bad sample is not
        # an API change.
        #
        # Order: the pinned id first (it is the right answer for a shared
        # alias), then the ids discovered from /v2/courses, then the bare call
        # as a last resort. Retry only on FAILURE — an empty list is a real
        # empty day (granby-ranch, rollingstone-ranch) and must report zero
        # rather than raise, which was the other half of the old bug.
        errors: list[Exception] = []

        def try_call(fids: str | None):
            """-> kenna's response, or None if this parameter shape failed."""
            try:
                return self._teetimes(alias, date, fids)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return None

        data = try_call(str(facility_id)) if facility_id else None
        discovery_failed = False
        if data is None:
            discovered = ""
            try:
                discovered = self._facility_ids(alias)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                discovery_failed = True
            if discovered and discovered != str(facility_id or ""):
                data = try_call(discovered)
        # The bare call is a last resort, but it is NOT a safe one for a row
        # with no pinned facility id. Some aliases answer it 200 with an empty
        # teetimes list (see the measurement above), so reaching it after the
        # facilities lookup FAILED converts a transport error into a clean
        # "nothing for sale" — and aggregate.py records that as courses_empty,
        # which sync() acts on by deactivating the date. The course then reads
        # as sold out rather than broken, and nothing ever flags it.
        #
        # Wicomico Shores (MD) is the measured case: kenna serves it 25-36
        # times a day and D1 had never held a single row for it. Run alone
        # through diag-course-pipeline the same adapter fetched 91 rows and
        # landed all of them, so the fetch path was never the problem — the
        # silent fallback was. A pinned facility id skips this hop entirely,
        # which is why that row now pins one; this guard is for every row that
        # does not.
        if data is None and not (discovery_failed and not facility_id):
            data = try_call(None)
        if data is None:
            raise errors[0] if errors else RuntimeError(
                f"{course['slug']}: teeitup returned nothing and raised nothing")

        return self._parse(course, data)

    def _parse(self, course: dict[str, Any], data: Any) -> list[TeeTime]:
        """Turn a raw kenna /v2/tee-times response into TeeTimes.

        Split out of fetch() so the residential-browser far fetcher
        (scraper/browser_teeitup.py) can reuse the exact ownership + timezone +
        sub-course-label logic on blocks it fetched itself. That fetcher primes
        `_FACILITIES[alias]` before calling this, so `_facility_meta` and
        `_pinned_course_ids` resolve from cache and no network happens here.
        """
        alias = course["ids"].get("alias")
        facility_id = course["ids"].get("facility_id")

        blocks = data if isinstance(data, list) else [data]
        meta = self._facility_meta(alias)
        state = course.get("state", "")
        state_tz = _STATE_TZ.get(state, _TZ_DEFAULT)
        # Announced once per course, and only when it actually bites: kenna
        # gave no timeZone AND this state has no entry, so every slot below is
        # about to be stamped with a coast somebody guessed. A new state's
        # first scrape is precisely when a silent default does its damage.
        unmapped_state = bool(state) and state not in _STATE_TZ

        # A pinned facility_id means this registry row is ONE course inside a
        # shared alias (seven facilities share city-of-phoenix-golf-courses),
        # so anything belonging to a sibling must be dropped or we would
        # publish another course's tee sheet under this name.
        #
        # Match on the MONGO courseId, never on the pinned integer directly:
        # slots have no facilityId key (diag_kenna_slots.txt). The integer is
        # accepted only where it genuinely appears, nested in each rate's
        # GolfNow distribution block, which keeps this working when the
        # facilities lookup 429s.
        pins = {p.strip() for p in str(facility_id).split(",")
                if p.strip()} if facility_id else set()
        want_cids = self._pinned_course_ids(alias, facility_id) if pins else set()

        def owned(slot: dict) -> bool:
            if str(slot.get("courseId")) in want_cids:
                return True
            for r in slot.get("rates") or []:
                gn = r.get("golfnow") if isinstance(r, dict) else None
                if isinstance(gn, dict) and str(gn.get("GolfFacilityId")) in pins:
                    return True
            return False

        all_slots = [s for block in blocks
                     for s in ((block or {}).get("teetimes", []) or [])]
        slots = [s for s in all_slots if owned(s)] if pins else list(all_slots)

        # Neither identity resolved, yet kenna sent slots. Dropping the sheet
        # is not the safe default — that is the failure this whole file is
        # commented for — but neither is publishing a sibling's times. The
        # response itself decides: kenna honours `facilityIds` server-side
        # (287 narrowed 304 slots to 55, one courseId), so a response spanning
        # exactly ONE course is unambiguous whoever filtered it.
        if pins and all_slots and not slots:
            cids = {str(s.get("courseId")) for s in all_slots}
            if len(cids) == 1:
                print(f"    {course['slug']}: pinned facility {sorted(pins)} did "
                      f"not resolve to a courseId, but the response spans one "
                      f"course ({cids.pop()[:8]}...) — keeping it")
                slots = list(all_slots)
            else:
                print(f"    {course['slug']}: pinned facility {sorted(pins)} did "
                      f"not resolve, and the response spans {len(cids)} courses "
                      f"— cannot attribute, reporting zero")

        # label slots per sub-course only when this response actually spans
        # multiple courses (multi-course facility like Hyland Hills)
        seen_cids = {str(s.get("courseId")) for s in slots if s.get("courseId")}
        multi = len(seen_cids) > 1

        out: list[TeeTime] = []
        for slot in slots:
            rates = slot.get("rates") or []
            cents = [r[k] for r in rates
                     for k in ("greenFeeWalking", "greenFeeCart")
                     if isinstance(r.get(k), (int, float))]
            holes = sorted({r.get("holes") for r in rates
                            if r.get("holes")})
            fmeta = meta.get(str(slot.get("courseId")), {})
            if unmapped_state and not fmeta.get("tz"):
                print(f"    {course['slug']}: kenna gave no timeZone and state "
                      f"{state!r} is not in _STATE_TZ — stamping "
                      f"{_TZ_DEFAULT}; add the state in "
                      f"scraper/adapters/teeitup.py")
                unmapped_state = False          # once per course, not per slot
            out.append(self.base_tee_time(
                course,
                teetime=self._to_local(slot.get("teetime", ""),
                                       fmeta.get("tz") or state_tz),
                course_label=(fmeta.get("name") or "") if multi else "",
                holes=[h for h in holes if h],
                # maxPlayers reflects how many can still book (probe: with
                # bookedPlayers=2 it reads 2, i.e. remaining seats)
                open_spots=slot.get("maxPlayers"),
                price_min=min(cents) / 100 if cents else None,
                price_max=max(cents) / 100 if cents else None,
                raw=slot,
            ))
        return out
