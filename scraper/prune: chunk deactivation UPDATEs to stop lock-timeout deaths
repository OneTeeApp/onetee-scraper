"""Cloudflare D1 storage for scraped tee times.

Writes go through the D1 HTTP API:
    POST https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query
    Authorization: Bearer <token>     body: {"sql": "...", "params": [...]}

Free-tier aware: D1's free plan allows 100,000 row writes/day, so this module
does DIFF-BASED sync — it reads the current rows for the scraped date (reads
are cheap: 5M/day) and only writes what changed:
  * INSERT rows for new tee times
  * UPDATE rows whose price/spots changed
  * mark rows active=0 when a slot disappeared (i.e. it got booked)
A typical re-scrape touches a small fraction of slots, so WRITES stay modest
at any cadence. READS are the number to watch since the 30-day tiered scan
landed: the near tier re-reads ~3 dates of rows every ~5 minutes, which works
out to roughly 8-10M row reads/day across all tiers — about 2x D1's free
5M/day. The initial 30-day far fill also writes ~150-200k rows in one sweep,
double the free 100k/day write cap. The tiered horizon therefore assumes the
$5/mo Workers Paid plan (25B reads + 50M writes/mo included); on the free
tier, raise scrape-near.yml's INTERVAL_SECONDS and stage the first far fill
across two days.

Env vars (set as GitHub Actions secrets):
    CLOUDFLARE_ACCOUNT_ID   CLOUDFLARE_API_TOKEN   CLOUDFLARE_D1_DB_ID

CLI:
    python -m scraper.d1 init                      # create tables (idempotent)
    python -m scraper.d1 push [--data FILE]        # diff-sync a scrape result
    python -m scraper.d1 stats                     # row counts / recent runs
Local development without Cloudflare: add --local test.db to any command and
the same logic runs against a local SQLite file instead of D1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

import requests

SCHEMA = (pathlib.Path(__file__).parent.parent / "schema.sql").read_text()

COLS = ["course_slug", "teetime", "course_label", "course_name", "city", "state",
        "venue_id", "source_role", "platform",
        "holes", "open_spots", "price_min", "price_max", "currency",
        "booking_url", "simulated", "active", "first_seen_at", "last_seen_at"]
CHUNK = 5   # rows per INSERT — D1's HTTP API caps bound params at 100/query
            # (5 rows × 19 cols = 95). Local SQLite would allow far more, but
            # correctness on D1 wins; initial full load is a one-time cost.
SLUG_CHUNK = 90   # slugs per read query (stays under D1's 100 bound-param cap)


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #

class D1Rest:
    """Cloudflare D1 over the HTTP API."""

    def __init__(self) -> None:
        acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        db = os.environ.get("CLOUDFLARE_D1_DB_ID")
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        if not all((acct, db, token)):
            sys.exit("Set CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_D1_DB_ID and "
                     "CLOUDFLARE_API_TOKEN (or use --local test.db).")
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
                    f"/d1/database/{db}/query")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def execute(self, sql: str, params: list | None = None) -> list[dict]:
        r = self.s.post(self.url, json={"sql": sql, "params": params or []},
                        timeout=30)
        body = r.json()
        if not body.get("success"):
            raise RuntimeError(f"D1 error: {body.get('errors')}")
        result = body["result"][0]
        # Rows touched by this statement, for callers that report write
        # tallies (migrate's rename counter read 0 forever because the
        # meta was discarded here).
        self.last_changes = int((result.get("meta") or {}).get("changes")
                                or 0)
        return result.get("results", [])

    def executescript(self, sql: str) -> None:
        # D1 accepts multi-statement sql when no params are bound
        self.execute(sql)


class SqliteLocal:
    """Same interface against a local SQLite file (dev/tests)."""

    def __init__(self, path: str) -> None:
        import sqlite3
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: list | None = None) -> list[dict]:
        cur = self.conn.execute(sql, params or [])
        self.conn.commit()
        # sqlite reports -1 for SELECTs; clamp so it mirrors D1Rest's meta.
        self.last_changes = max(0, cur.rowcount)
        return [dict(r) for r in cur.fetchall()]

    def executescript(self, sql: str) -> None:
        self.conn.executescript(sql)
        self.conn.commit()


class HttpBackend:
    """Same interface as D1Rest, but POSTs to the OneTee VPS /exec endpoint,
    which mirrors D1's REST API and translates SQLite->Postgres. Used for
    dual-write during the D1->VPS migration. Env: ONETEE_API_URL (e.g.
    https://api.oneteeapp.com) + ONETEE_INGEST_TOKEN."""

    def __init__(self) -> None:
        base = os.environ.get("ONETEE_API_URL")
        token = os.environ.get("ONETEE_INGEST_TOKEN")
        if not base or not token:
            raise RuntimeError("Set ONETEE_API_URL and ONETEE_INGEST_TOKEN")
        self.url = base.rstrip("/") + "/exec"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.last_changes = 0

    def execute(self, sql: str, params: list | None = None) -> list[dict]:
        r = self.s.post(self.url, json={"sql": sql, "params": params or []},
                        timeout=60)
        if r.status_code >= 400:
            # Surface the server's error body (Postgres message + SQLSTATE), not
            # just the generic "500 Server Error" — makes mirror failures
            # diagnosable straight from the scraper log.
            body = (r.text or "")[:300].replace("\n", " ")
            raise RuntimeError(f"HTTP {r.status_code}: {body}")
        return (r.json() or {}).get("results", [])

    def executescript(self, sql: str) -> None:
        # The VPS schema is deploy-managed: vps/deploy.sh applies schema.sql +
        # accounts-schema.sql on every deploy, and the /exec endpoint already
        # skips DDL (CREATE/ALTER/DROP) on purpose. Replaying SCHEMA here is
        # therefore a no-op in intent — and worse, schema.sql interleaves SQL
        # comments, so a naive split-on-";" hands Postgres comment-prefixed
        # fragments that slip past /exec's DDL skip and raise 42601 ("syntax
        # error at end of input"), killing the push. Treat schema DDL as a
        # no-op against the VPS, exactly like DualBackend does for its mirror.
        return


class DualBackend:
    """Runs every statement on `primary` (the real store — D1) and MIRRORS
    writes to `secondary` (the VPS) non-fatally. Reads (SELECT) hit primary
    only; schema DDL (executescript) is NOT mirrored (the VPS schema is managed
    by its own deploy). A VPS failure is logged and swallowed, so dual-write can
    never break the live D1 path."""

    def __init__(self, primary, secondary) -> None:
        self.primary = primary
        self.secondary = secondary
        self.last_changes = 0

    def execute(self, sql: str, params: list | None = None) -> list[dict]:
        res = self.primary.execute(sql, params)
        self.last_changes = getattr(self.primary, "last_changes", 0)
        if not sql.lstrip().upper().startswith("SELECT"):
            try:
                self.secondary.execute(sql, params)
            except Exception as e:  # noqa: BLE001
                print(f"[vps-dualwrite] mirror failed: {e}", file=sys.stderr)
        return res

    def executescript(self, sql: str) -> None:
        # VPS schema is deploy-managed; only run DDL against the primary.
        self.primary.executescript(sql)


# --------------------------------------------------------------------------- #
# sync logic
# --------------------------------------------------------------------------- #

def init_schema(db) -> None:
    # Forward-compatible + idempotent: add any newer columns to a pre-existing
    # table BEFORE running SCHEMA (whose indexes reference them). On a fresh DB
    # each ALTER no-ops (no table yet) and SCHEMA creates the table with the
    # columns already in it. Each ALTER is independent so one existing column
    # doesn't skip the others. Runs cheaply on every push.
    for ddl in (
        "ALTER TABLE tee_times ADD COLUMN state TEXT",
        "ALTER TABLE tee_times ADD COLUMN venue_id TEXT",
        "ALTER TABLE tee_times ADD COLUMN source_role TEXT DEFAULT 'primary'",
        "ALTER TABLE tee_times ADD COLUMN course_label TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tee_times ADD COLUMN became_active_at TEXT",
    ):
        try:
            db.execute(ddl)
        except Exception:  # noqa: BLE001 — column already exists, or fresh DB
            pass
    _rebuild_pk_if_legacy(db)
    db.executescript(SCHEMA)


def _rebuild_pk_if_legacy(db) -> None:
    """One-time table rebuild when the PK is the legacy (course_slug, teetime).

    A 3-course facility has three real 7:00 slots; the 2-column key collapsed
    them via INSERT OR REPLACE. SQLite can't ALTER a PK, so detect via
    PRAGMA table_info (pk column count) and rebuild: create the new-shape
    table, copy rows, swap. Legacy teeitup rows are NOT copied — they carry
    raw-UTC timestamps (".000Z", hours off local) and are fully rewritten by
    the next scrape anyway. Idempotent: after the rebuild pk count is 3.
    """
    try:
        info = db.execute("PRAGMA table_info(tee_times)")
    except Exception:  # noqa: BLE001 — fresh DB, no table yet
        return
    pk_cols = [r["name"] for r in info if r.get("pk")]
    if not pk_cols or len(pk_cols) >= 3:
        return                            # fresh (SCHEMA will create) or done
    cols = ",".join(COLS)
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS tee_times_v2 (
          course_slug  TEXT NOT NULL,
          teetime      TEXT NOT NULL,
          course_label TEXT NOT NULL DEFAULT '',
          course_name  TEXT NOT NULL,
          city         TEXT,
          state        TEXT,
          venue_id     TEXT,
          source_role  TEXT DEFAULT 'primary',
          platform     TEXT,
          holes        TEXT,
          open_spots   INTEGER,
          price_min    REAL,
          price_max    REAL,
          currency     TEXT DEFAULT 'USD',
          booking_url  TEXT,
          simulated    INTEGER DEFAULT 0,
          active       INTEGER DEFAULT 1,
          first_seen_at TEXT NOT NULL,
          last_seen_at  TEXT NOT NULL,
          PRIMARY KEY (course_slug, teetime, course_label)
        );
        INSERT OR IGNORE INTO tee_times_v2 ({cols})
          SELECT course_slug, teetime, COALESCE(course_label,''), course_name,
                 city, state, venue_id, COALESCE(source_role,'primary'),
                 platform, holes, open_spots, price_min, price_max,
                 COALESCE(currency,'USD'), booking_url,
                 COALESCE(simulated,0), COALESCE(active,1),
                 first_seen_at, last_seen_at
            FROM tee_times
           WHERE NOT (platform = 'teeitup' AND teetime LIKE '%Z')
             AND teetime >= strftime('%Y-%m-%dT00:00:00', 'now', '-1 day');
        DROP TABLE tee_times;
        ALTER TABLE tee_times_v2 RENAME TO tee_times;
    """)


def migrate(db, registry_path: str | None = None) -> dict:
    """Idempotent forward migration: ensure schema (incl. the state / venue_id /
    source_role columns and indexes) and backfill them on legacy rows from the
    registry. Backfill is keyed per course_slug so re-tagged sources (a course
    split into native primary + GolfNow supplement) get the right venue grouping
    even on rows written before those columns existed. Safe to run repeatedly.

    ALSO REPAIRS A RENAMED COURSE. sync() writes course_name only on INSERT —
    an existing row's UPDATE covers open_spots, prices and active, nothing else
    — so renaming a course leaves every row already in D1 showing the old name
    until it happens to churn off the horizon. That can take the full 30 days,
    during which the directory card and the tee-time card disagree about what
    the course is called. This backfill closes that window; it is a no-op when
    the names already agree."""
    init_schema(db)
    backfilled = 0
    renamed = 0
    if registry_path:
        import pathlib as _p
        reg = json.loads(_p.Path(registry_path).read_text())["courses"]
        for c in reg:
            slug = c["slug"]
            db.execute(
                "UPDATE tee_times SET state=?, venue_id=?, source_role=? "
                "WHERE course_slug=? AND (venue_id IS NULL OR venue_id='' "
                "OR state IS NULL OR state='')",
                [c.get("state", ""), c.get("venue_id") or slug,
                 c.get("source_role", "primary"), slug])
            backfilled += 1
            shown = (c.get("display_name") or "").strip() or c["name"]
            db.execute(
                "UPDATE tee_times SET course_name=? "
                "WHERE course_slug=? AND course_name<>?",
                [shown, slug, shown])
            if getattr(db, "last_changes", 0):
                renamed += 1
    return {"backfilled_courses": backfilled, "courses_renamed": renamed}


# --------------------------------------------------------------------------- #
# past-slot pruning
# --------------------------------------------------------------------------- #

# A tee time that has already started can't be booked. Two things let elapsed
# slots linger as active=1:
#   * sync() only deactivates rows for courses present in THAT scrape, so a
#     course that errored (or sat in a shard that failed) keeps its old rows;
#   * between runs, time simply passes — a 7:20am slot is stale by 7:21am even
#     though the 7:06am scrape that wrote it was perfectly correct.
# The read API also filters past slots, but that only helps once the Worker is
# deployed. Pruning at the DATA layer makes every consumer correct.
#
# Times are stored as naive LOCAL course time, so "past" must be evaluated in
# each course's own timezone.
_STATE_TZ = {
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/New_York",
    "NH": "America/New_York", "NJ": "America/New_York", "NY": "America/New_York",
    "NC": "America/New_York", "OH": "America/New_York", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "VT": "America/New_York",
    "VA": "America/New_York", "WV": "America/New_York", "IN": "America/New_York",
    "KY": "America/New_York",
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "LA": "America/Chicago",
    "MN": "America/Chicago", "MS": "America/Chicago", "MO": "America/Chicago",
    "NE": "America/Chicago", "ND": "America/Chicago", "OK": "America/Chicago",
    "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "WI": "America/Chicago",
    "CO": "America/Denver", "MT": "America/Denver", "NM": "America/Denver",
    "UT": "America/Denver", "WY": "America/Denver", "ID": "America/Denver",
    "AZ": "America/Phoenix",          # no DST
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}
# Florida straddles two timezones: the panhandle west of the Apalachicola
# River is Central. Judging those rows by New_York pruned (and, in the Worker,
# hid) their next hour of bookable slots ALL DAY — every sync re-activated
# them and the next prune flipped them off again. City-level carve-out because
# rows carry city but not county; mirror any edit in worker/index.js
# FL_CENTRAL_CITIES. (Port St. Joe, Carrabelle and Tallahassee are Eastern.)
FL_CENTRAL_CITIES = {
    "Bonifay", "Crestview", "DeFuniak Springs", "Destin", "Fort Walton Beach", "Freeport",
    "Gulf Breeze", "Hurlburt Field", "Lynn Haven", "Milton", "Miramar Beach",
    "Navarre", "Niceville", "Pace", "Panama City", "Panama City Beach",
    "Pensacola", "Shalimar", "Sunny Hills", "Watersound",
}
# Rows with no state yet (legacy inserts) are pruned only once the slot is past
# in the LAST US timezone to get there — conservative: never hides a bookable
# slot, at the cost of leaving a few stale ones visible a bit longer.
_FALLBACK_TZ = "Pacific/Honolulu"


def deactivate_unknown_slugs(db, registry_path: str,
                             dry_run: bool = False) -> dict:
    """Set active=0 on rows whose course_slug is no longer in the registry.

    sync() deactivates a row only when its course is IN the scrape it is
    diffing (`k[0] in scraped_courses`). That is deliberate — a course that
    errored, or that sits in a shard which failed, must keep its rows rather
    than blink off the site. But it leaves a gap with no other closer: a slug
    that has LEFT the registry is in no scrape ever again, so nothing revisits
    its rows and they stay active until each slot individually elapses.

    Measured cost of that gap (probe-results/co_frontend.txt, 2026-07-25):
    135 active rows across three slugs that registry.json has never heard of —
    `gold-canyon-golf-resort-dinosaur-mountain-sidewinder`,
    `grayhawk-golf-club-raptor-talon`,
    `mountain-view-golf-course-fort-huachuca`. All three are pre-`course_label`
    per-sub-course slugs; their real courses are in the registry under the base
    slug. They were also every single row in D1 with no `state`, because state
    is written from the registry row and these have none — which is how three
    Arizona ghosts ended up filed under Colorado by the widget's city-inference
    fallback.

    So this is NOT a state backfill. Giving these rows a state would keep
    phantom courses on the site with a tidier label. They have to go inactive.

    The registry — not the current scrape — is the authority here, which is
    what makes this safe under sharding: a course present in registry.json but
    absent from this shard is untouched. Only slugs the registry does not
    contain at all are closed out.
    """
    import pathlib as _p
    doc = json.loads(_p.Path(registry_path).read_text())
    known = {c["slug"] for c in (doc["courses"] if isinstance(doc, dict)
                                 else doc)}
    if not known:                      # an unreadable registry must not wipe D1
        raise RuntimeError(f"{registry_path} yielded no course slugs — "
                           f"refusing to deactivate anything")

    rows = db.execute("SELECT course_slug, COUNT(*) AS n FROM tee_times "
                      "WHERE active = 1 GROUP BY course_slug")
    orphans = {r["course_slug"]: r["n"] for r in rows
               if r["course_slug"] not in known}
    if orphans and not dry_run:
        slugs = sorted(orphans)
        for i in range(0, len(slugs), SLUG_CHUNK):
            batch = slugs[i:i + SLUG_CHUNK]
            ph = ",".join("?" * len(batch))
            _prune_chunked(db, f"active = 1 AND course_slug IN ({ph})", batch)
    return {"deactivated": sum(orphans.values()), "slugs": orphans,
            "known_courses": len(known), "dry_run": dry_run}


def deactivate_courses(db, slugs, dry_run: bool = False) -> dict:
    """Set active=0 on every active row of the named courses.

    The closer for the OTHER gap in sync(). `deactivate_unknown_slugs` handles
    a slug that left the registry; this handles one that is still in it and
    still being fetched, but has stopped returning anything. Such a course
    never enters `scraped_courses`, so sync() never reconciles it, and its last
    tee sheet stays active=1 — served to golfers as current availability —
    until each slot elapses on its own.

    This does NOT decide which courses those are, and that separation is the
    point. It takes an adjudicated list. `scripts/probe_staleness.py` produces
    one by fetching each candidate live and requiring a clean, error-free,
    zero-row answer on every date we are serving; an adapter raising is
    explicitly not evidence a course went dark. Passing a slug here asserts
    that somebody — a probe or a human — established it.

    Safe to be wrong in one direction: the next successful scrape re-INSERTs
    the rows (sync() reactivates on `not e["active"]`), so a wrongly-closed
    course is back within the hour, while a wrongly-kept one lies for days.
    """
    slugs = sorted(set(slugs))
    if not slugs:
        return {"deactivated": 0, "slugs": {}, "dry_run": dry_run}

    counts: dict[str, int] = {}
    for i in range(0, len(slugs), SLUG_CHUNK):
        batch = slugs[i:i + SLUG_CHUNK]
        ph = ",".join("?" * len(batch))
        for r in db.execute(
                f"SELECT course_slug, COUNT(*) AS n FROM tee_times "
                f"WHERE active = 1 AND course_slug IN ({ph}) "
                f"GROUP BY course_slug", batch):
            counts[r["course_slug"]] = r["n"]
        if not dry_run:
            _prune_chunked(db, f"active = 1 AND course_slug IN ({ph})", batch)
    return {"deactivated": sum(counts.values()), "slugs": counts,
            "dry_run": dry_run}


def _prune_chunked(db, where: str, params: list, chunk: int = 5000) -> int:
    """Deactivate matching rows in small batches. The single broad UPDATE
    fought the ingest fleet for row locks and died on lock_timeout (SQLSTATE
    55P03 through the /exec 4s cap) once the table grew; each chunk locks
    only a sliver, finishes fast, and interleaves with ingest instead.
    [revert: db.execute("UPDATE tee_times SET active = 0 WHERE " + where,
    params) at each call site]"""
    total = 0
    for _ in range(2000):          # hard stop: 10M rows per call site
        rows = db.execute(
            f"UPDATE tee_times SET active = 0 WHERE ctid IN ("
            f"SELECT ctid FROM tee_times WHERE {where} LIMIT {chunk}) "
            f"RETURNING 1", params)
        total += len(rows)
        if len(rows) < chunk:
            break
    return total


def _local_now(tz_name: str) -> str:
    import zoneinfo
    return (dt.datetime.now(zoneinfo.ZoneInfo(tz_name))
            .replace(tzinfo=None).isoformat(timespec="seconds"))


def prune_past(db, dry_run: bool = False) -> dict:
    """Set active=0 on every active row whose tee time already elapsed in the
    course's local timezone. Idempotent and cheap — when nothing is stale it
    costs one read per timezone group and zero writes."""
    by_tz: dict[str, list[str]] = {}
    for st, tz in _STATE_TZ.items():
        by_tz.setdefault(tz, []).append(st)

    total, per_tz = 0, {}
    city_marks = ",".join("?" * len(FL_CENTRAL_CITIES))
    fl_central = sorted(FL_CENTRAL_CITIES)
    for tz, states in sorted(by_tz.items()):
        now = _local_now(tz)
        marks = ",".join("?" * len(states))
        where = f"active = 1 AND state IN ({marks}) AND teetime < ?"
        params = [*states, now]
        if tz == "America/New_York":
            # Panhandle FL is Central — judged separately below, and it must
            # NOT be pruned an hour early by the Eastern clock here.
            where += (f" AND NOT (state = 'FL' "
                      f"AND COALESCE(city,'') IN ({city_marks}))")
            params += fl_central
        n = db.execute(f"SELECT COUNT(*) AS n FROM tee_times WHERE {where}",
                       params)[0]["n"]
        if n:
            if not dry_run:
                _prune_chunked(db, where, params)
            per_tz[tz] = n
            total += n

    now = _local_now("America/Chicago")     # panhandle FL, on its own clock
    where = (f"active = 1 AND state = 'FL' "
             f"AND COALESCE(city,'') IN ({city_marks}) AND teetime < ?")
    n = db.execute(f"SELECT COUNT(*) AS n FROM tee_times WHERE {where}",
                   [*fl_central, now])[0]["n"]
    if n:
        if not dry_run:
            _prune_chunked(db, where, [*fl_central, now])
        per_tz["America/Chicago (FL panhandle)"] = n
        total += n

    now = _local_now(_FALLBACK_TZ)          # unknown / blank state
    where = "active = 1 AND (state IS NULL OR state = '') AND teetime < ?"
    n = db.execute(f"SELECT COUNT(*) AS n FROM tee_times WHERE {where}",
                   [now])[0]["n"]
    if n:
        if not dry_run:
            _prune_chunked(db, where, [now])
        per_tz["(no state)"] = n
        total += n

    # Housekeeping: drop freshness rows for dates that are wholly in the past
    # (UTC is safe — a date is past for every US tz once it is past in UTC minus
    # a day of slack). Keeps the ledger to ~courses×31, self-limiting.
    if not dry_run:
        try:
            cutoff = (dt.datetime.now(dt.timezone.utc).date()
                      - dt.timedelta(days=1)).isoformat()
            db.execute("DELETE FROM sheet_freshness WHERE date < ?", [cutoff])
        except Exception:  # noqa: BLE001 — table may not exist on a legacy DB
            pass

    return {"deactivated": total, "by_tz": per_tz, "dry_run": dry_run}


def _key(t: dict) -> tuple[str, str, str]:
    return (t["course_slug"], t["teetime"], t.get("course_label") or "")


def sync(db, doc: dict) -> dict:
    """Diff-sync one aggregate result document into the tee_times table."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    date = doc["date"]
    scraped = {}
    for t in doc["tee_times"]:
        scraped[_key(t)] = {
            "course_slug": t["course_slug"], "teetime": t["teetime"],
            "course_label": t.get("course_label") or "",
            "course_name": t["course_name"], "city": t.get("city"),
            "state": t.get("state"),
            "venue_id": t.get("venue_id") or t["course_slug"],
            "source_role": t.get("source_role") or "primary",
            "platform": t.get("platform"),
            "holes": "/".join(map(str, t.get("holes") or [])),
            "open_spots": t.get("open_spots"),
            "price_min": t.get("price_min"), "price_max": t.get("price_max"),
            "currency": t.get("currency", "USD"),
            "booking_url": t.get("booking_url"),
            "simulated": 1 if t.get("simulated") else 0,
            "active": 1, "first_seen_at": now, "last_seen_at": now,
        }

    # courses that errored this run must NOT have their rows deactivated.
    # A record WITHOUT course_label shields the whole course; a record WITH
    # one (a PartialFetchError: one sub-course failed while siblings served)
    # shields only that sheet's rows, so the venue's served sheets still
    # reconcile normally.
    errored = {e["course"] for e in doc.get("errors", [])
               if not e.get("course_label")}
    label_errored = {(e["course"], e["course_label"])
                     for e in doc.get("errors", []) if e.get("course_label")}
    scraped_courses = {k[0] for k in scraped}
    # A course that answered cleanly with ZERO rows for this date is evidence,
    # not ignorance: the day sold out or its booking window closed, and its
    # leftover rows for this date must be closed out like any other vanished
    # slot. aggregate.py has recorded these in courses_empty since the 30-day
    # horizon landed; older docs lack the key and keep the old (conservative)
    # behaviour. Belt-and-braces: never let an errored course in via this set.
    empty_ok = {c for c in doc.get("courses_empty", []) if c not in errored}
    scraped_courses |= empty_ok

    # Read existing rows ONLY for the courses in this document (a shard's slice),
    # not the whole date. Deactivation only ever touches scraped_courses, so this
    # is behaviour-preserving — and it keeps each sync O(shard) instead of
    # O(all courses in the DB), which is what makes 15k courses viable. Chunk the
    # IN-list to stay under D1's 100 bound-param limit.
    existing: dict = {}
    slugs = sorted(scraped_courses)
    for i in range(0, len(slugs), SLUG_CHUNK):
        batch = slugs[i:i + SLUG_CHUNK]
        ph = ",".join("?" * len(batch))
        for r in db.execute(
                "SELECT course_slug, teetime, course_label, open_spots, "
                f"price_min, price_max, active, booking_url, platform "
                f"FROM tee_times "
                f"WHERE substr(teetime,1,10) = ? "
                f"AND course_slug IN ({ph})", [date, *batch]):
            existing[(r["course_slug"], r["teetime"],
                      r.get("course_label") or "")] = r

    to_insert = [v for k, v in scraped.items() if k not in existing]
    to_update = []
    for k, v in scraped.items():
        e = existing.get(k)
        # booking_url/platform are part of the diff: when a course moves
        # booking engines (or an adapter's link format is fixed), the rows
        # already in D1 must pick up the new link — the old UPDATE never
        # refreshed it, so a re-platformed course served dead booking links
        # for up to 30 days.
        if e and (e["open_spots"] != v["open_spots"]
                  or e["price_min"] != v["price_min"]
                  or e["price_max"] != v["price_max"]
                  or e.get("booking_url") != v["booking_url"]
                  or e.get("platform") != v["platform"]
                  or not e["active"]):
            # A row returning from active=0 is a CANCELLATION: the slot was
            # booked and has just been freed. first_seen_at deliberately keeps
            # its original stamp, so that signal needs a column of its own.
            v["_reactivated"] = not e["active"]
            to_update.append(v)
    to_deactivate = [k for k, e in existing.items()
                     if e["active"] and k not in scraped
                     and k[0] in scraped_courses and k[0] not in errored
                     and (k[0], k[2]) not in label_errored]

    for i in range(0, len(to_insert), CHUNK):
        chunk = to_insert[i:i + CHUNK]
        placeholders = ",".join("(" + ",".join("?" * len(COLS)) + ")"
                                for _ in chunk)
        params = [row[c] for row in chunk for c in COLS]
        db.execute(f"INSERT OR REPLACE INTO tee_times ({','.join(COLS)}) "
                   f"VALUES {placeholders}", params)

    for row in to_update:
        if row.get("_reactivated"):
            db.execute(
                "UPDATE tee_times SET open_spots=?, price_min=?, price_max=?, "
                "booking_url=?, platform=?, active=1, last_seen_at=?, "
                "became_active_at=? "
                "WHERE course_slug=? AND teetime=? AND course_label=?",
                [row["open_spots"], row["price_min"], row["price_max"],
                 row["booking_url"], row["platform"], now, now,
                 row["course_slug"], row["teetime"], row["course_label"]])
        else:
            db.execute(
                "UPDATE tee_times SET open_spots=?, price_min=?, price_max=?, "
                "booking_url=?, platform=?, active=1, last_seen_at=? "
                "WHERE course_slug=? AND teetime=? AND course_label=?",
                [row["open_spots"], row["price_min"], row["price_max"],
                 row["booking_url"], row["platform"],
                 now, row["course_slug"], row["teetime"], row["course_label"]])

    for slug, teetime, label in to_deactivate:
        db.execute("UPDATE tee_times SET active=0, last_seen_at=? "
                   "WHERE course_slug=? AND teetime=? AND course_label=?",
                   [now, slug, teetime, label])

    # Freshness ledger: stamp every course we CONFIRMED for this date — those
    # that returned rows plus those that returned a trustworthy clean empty
    # (`scraped_courses` already = keyed rows ∪ courses_empty, and both exclude
    # errored/label-errored courses). A course that errored is deliberately NOT
    # stamped, so its last_ok_at goes stale and the read API stops showing its
    # (shielded, still-active) rows until a scrape confirms them again. This is
    # what turns a stalled scraper into "no slots" instead of "phantom slots".
    # Batched: 3 cols, ≤30 rows/statement stays under D1's 100-param cap.
    confirmed = sorted(c for c in scraped_courses if c not in errored)
    for i in range(0, len(confirmed), 30):
        batch = confirmed[i:i + 30]
        values = ",".join("(?,?,?)" for _ in batch)
        params = [p for c in batch for p in (c, date, now)]
        db.execute(
            f"INSERT INTO sheet_freshness (course_slug, date, last_ok_at) "
            f"VALUES {values} "
            f"ON CONFLICT(course_slug, date) DO UPDATE SET "
            f"last_ok_at=excluded.last_ok_at", params)

    stats = {"rows_inserted": len(to_insert), "rows_updated": len(to_update),
             "rows_deactivated": len(to_deactivate)}
    db.execute(
        "INSERT INTO runs (generated_at, date, courses_queried, courses_ok, "
        "tee_times, rows_inserted, rows_updated, rows_deactivated, errors) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [doc["generated_at"], date, doc.get("courses_queried"),
         doc.get("courses_ok"), len(doc["tee_times"]), stats["rows_inserted"],
         stats["rows_updated"], stats["rows_deactivated"],
         json.dumps(doc.get("errors", []))])
    return stats


# --------------------------------------------------------------------------- #
# coverage — the "landed 0" health signal
# --------------------------------------------------------------------------- #

def coverage(db, window_hours: float = 12.0,
             registry_path: str | None = None) -> list[dict]:
    """Per-platform freshness coverage — the signal for a silently-dead scraper.

    A broken scraper looks exactly like a healthy one: the workflow goes green,
    it just writes no rows. What tells the two apart is the sheet_freshness
    ledger: sync() stamps last_ok_at for every course it CONFIRMED (rows OR a
    trustworthy clean empty) and NEVER for a course that errored. So "was this
    platform scraped successfully lately?" = "did any of its courses get a fresh
    ledger stamp?" — which is true even when a platform legitimately has no
    availability, and false the moment its scraper breaks.

    Denominator = courses that have EVER produced inventory (DISTINCT
    course_slug per platform in tee_times; rows are only deactivated, never
    deleted, so a platform stays counted after it goes dark), INTERSECTED with
    the current registry when `registry_path` is given. The intersect drops
    "ghost" (course_slug, platform) pairs — old platform assignments whose rows
    linger in D1 after a course churned platforms, got reclassified unsupported,
    or had its slug retired/consolidated. Those are stale data, not missing
    coverage, and without this filter they inflate every dark count. Numerator =
    those whose most recent clean scrape (any date) is within `window_hours`.

    Returns one dict per platform: {platform, known, fresh, pct, stale_courses},
    worst first.
    """
    from collections import defaultdict
    known: dict[str, set] = defaultdict(set)
    for r in db.execute(
            "SELECT DISTINCT course_slug, platform FROM tee_times "
            "WHERE platform IS NOT NULL AND platform != ''"):
        known[r["platform"]].add(r["course_slug"])

    if registry_path:
        try:
            reg = json.loads(pathlib.Path(registry_path).read_text())
            current = defaultdict(set)
            for c in reg.get("courses", reg if isinstance(reg, list) else []):
                p = c.get("platform")
                if p:
                    current[p].add(c["slug"])
            known = {p: (slugs & current.get(p, set()))
                     for p, slugs in known.items()}
            known = {p: s for p, s in known.items() if s}  # drop emptied platforms
        except (OSError, ValueError, KeyError):
            pass                 # no registry checkout — fall back to tee_times only

    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(hours=window_hours)).isoformat(timespec="seconds")
    fresh: set = set()
    for r in db.execute("SELECT course_slug, MAX(last_ok_at) AS last_ok "
                        "FROM sheet_freshness GROUP BY course_slug"):
        if (r["last_ok"] or "") >= cutoff:
            fresh.add(r["course_slug"])

    out = []
    for platform, courses in known.items():
        total = len(courses)
        stale = sorted(courses - fresh)
        out.append({"platform": platform, "known": total,
                    "fresh": total - len(stale),
                    "pct": round(100.0 * (total - len(stale)) / total, 1)
                    if total else 0.0,
                    "stale_courses": stale})
    out.sort(key=lambda d: (d["pct"], -d["known"]))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="Cloudflare D1 tee-time store")
    p.add_argument("cmd",
                   choices=["init", "migrate", "push", "prune", "stats",
                            "coverage"])
    p.add_argument("--dry-run", action="store_true",
                   help="prune: report what would be deactivated, write nothing")
    p.add_argument("--data", default="output/tee_times.json")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--local", metavar="SQLITE_FILE",
                   help="use a local SQLite file instead of Cloudflare D1")
    # coverage flags
    p.add_argument("--window-hours", type=float, default=12.0,
                   help="coverage: a course is 'fresh' if cleanly scraped within "
                        "this many hours (default 12)")
    p.add_argument("--min-courses", type=int, default=4,
                   help="coverage --alert: only a platform with at least this "
                        "many known courses can trip the alert (default 4)")
    p.add_argument("--warn-pct", type=float, default=25.0,
                   help="coverage: platforms below this %% fresh are flagged as "
                        "degraded (warning, not failure) (default 25)")
    p.add_argument("--alert", action="store_true",
                   help="coverage: exit non-zero if any sizable platform is fully "
                        "dark (0 fresh) — turns the workflow red so GitHub emails")
    p.add_argument("--exclude", default="",
                   help="coverage: comma-separated platforms to report but never "
                        "fail on (e.g. a known-unfixable source)")
    p.add_argument("--list-dark", action="store_true",
                   help="coverage: also list the specific not-fresh course slugs "
                        "behind each platform's %% (the per-course gaps to triage)")
    a = p.parse_args()

    if a.local:
        db = SqliteLocal(a.local)
    elif os.environ.get("VPS_ONLY") == "1":
        # Cutover: the VPS Postgres is the sole store — D1 is never touched
        # (no D1 credentials needed, no D1 writes billed). Reads + writes both
        # go through the /exec HTTP backend.
        db = HttpBackend()
        print("VPS-ONLY backend (D1 disabled)", file=sys.stderr)
    else:
        db = D1Rest()
        # Migration dual-write: mirror writes to the VPS when explicitly enabled.
        # Inert unless VPS_DUALWRITE=1 is set on the workflow — zero risk to D1.
        if os.environ.get("VPS_DUALWRITE") == "1":
            try:
                db = DualBackend(db, HttpBackend())
                print("VPS dual-write ENABLED", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"VPS dual-write init failed, continuing D1-only: {e}",
                      file=sys.stderr)

    if a.cmd == "init":
        init_schema(db)
        print("schema ensured")
    elif a.cmd == "migrate":
        s = migrate(db, a.registry)
        print(f"migrated: state/venue_id/source_role columns ensured, indexes "
              f"created, backfill touched {s['backfilled_courses']} courses")
        # Backfill can only fix rows whose course is still in the registry.
        # Rows for a slug that has LEFT it need closing out, not tidying up.
        o = deactivate_unknown_slugs(db, a.registry)
        print(f"deactivated {o['deactivated']} rows for "
              f"{len(o['slugs'])} slug(s) no longer in the registry: "
              f"{o['slugs'] or '(none)'}")
    elif a.cmd == "push":
        init_schema(db)
        doc = json.loads(pathlib.Path(a.data).read_text())
        s = sync(db, doc)
        print(f"synced {a.data} for {doc['date']}: "
              f"+{s['rows_inserted']} inserted, ~{s['rows_updated']} updated, "
              f"-{s['rows_deactivated']} deactivated "
              f"(total writes ≈ {sum(s.values()) + 1})")
        # Elapsed slots can't be booked. Prune on every push so the data is
        # correct for any consumer, not just a Worker build that filters.
        pr = prune_past(db)
        print(f"pruned {pr['deactivated']} elapsed rows {pr['by_tz']}")
    elif a.cmd == "prune":
        pr = prune_past(db, dry_run=a.dry_run)
        verb = "would deactivate" if a.dry_run else "deactivated"
        print(f"{verb} {pr['deactivated']} elapsed rows {pr['by_tz']}")
        # The hourly backstop is also the right cadence for retired slugs:
        # nothing else ever revisits a course the registry has dropped.
        o = deactivate_unknown_slugs(db, a.registry, dry_run=a.dry_run)
        print(f"{verb} {o['deactivated']} rows for {len(o['slugs'])} slug(s) "
              f"no longer in the registry (of {o['known_courses']} known): "
              f"{o['slugs'] or '(none)'}")
    elif a.cmd == "stats":
        total = db.execute("SELECT COUNT(*) AS n, SUM(active) AS act FROM tee_times")
        runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")
        print("tee_times rows:", total[0])
        for r in runs:
            print(" run:", dict(r))
    elif a.cmd == "coverage":
        rows = coverage(db, window_hours=a.window_hours,
                        registry_path=a.registry)
        excluded = {p.strip() for p in a.exclude.split(",") if p.strip()}
        print(f"Per-platform freshness coverage "
              f"(fresh = cleanly scraped within {a.window_hours:g}h):")
        print(f"  {'platform':<16} {'fresh/known':>12}  {'pct':>6}  status")
        dark, degraded = [], []
        for r in rows:
            sizable = r["known"] >= a.min_courses
            if sizable and r["fresh"] == 0 and r["platform"] not in excluded:
                status = "DARK ***"
                dark.append(r)
            elif sizable and r["fresh"] == 0 and r["platform"] in excluded:
                status = "dark (excluded)"
            elif r["pct"] < a.warn_pct:
                status = "degraded"
                if sizable:
                    degraded.append(r)
            else:
                status = "ok"
            print(f"  {r['platform']:<16} "
                  f"{str(r['fresh']) + '/' + str(r['known']):>12}  "
                  f"{r['pct']:>5}%  {status}")
        if degraded:
            print("\nDEGRADED (below "
                  f"{a.warn_pct:g}% fresh — worth a look, not failing):")
            for r in degraded:
                print(f"  - {r['platform']}: {r['fresh']}/{r['known']} "
                      f"({r['pct']}%)")
        if dark:
            print(f"\n*** LANDED-ZERO ALERT: {len(dark)} platform(s) fully DARK "
                  f"(0 of ≥{a.min_courses} known courses scraped in "
                  f"{a.window_hours:g}h) ***")
            for r in dark:
                print(f"  - {r['platform']}: 0/{r['known']} courses fresh")
            print("A whole platform that normally produces has stopped. See "
                  "docs/ARCHITECTURE.md §7 (green run → 0 rows) to diagnose.")
            if a.alert:
                return 1
        else:
            print("\nAll sizable platforms have fresh coverage. No dark platforms.")
        if a.list_dark:
            gaps = [r for r in rows if r.get("stale_courses")]
            if gaps:
                print("\nPer-course gaps (known courses not freshly scraped in "
                      f"{a.window_hours:g}h — triage: bad/stale IDs vs delisted "
                      "vs genuinely-empty-but-ledger-lagging):")
                for r in gaps:
                    slugs = r["stale_courses"]
                    shown = ", ".join(slugs[:20])
                    more = f" … (+{len(slugs) - 20} more)" if len(slugs) > 20 else ""
                    print(f"  {r['platform']} ({len(slugs)}): {shown}{more}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
