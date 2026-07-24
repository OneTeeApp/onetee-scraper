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
A typical hourly re-scrape touches a small fraction of slots, so 24 runs/day
fits the free tier with room to spare. A full first run writes every row
(~5k for all of Colorado) — still fine.

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

COLS = ["course_slug", "teetime", "course_name", "city", "state", "platform",
        "holes", "open_spots", "price_min", "price_max", "currency",
        "booking_url", "simulated", "active", "first_seen_at", "last_seen_at"]
CHUNK = 6   # rows per INSERT — D1's HTTP API caps bound params at 100/query
            # (6 rows × 16 cols = 96). Local SQLite would allow far more, but
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
        return body["result"][0].get("results", [])

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
        return [dict(r) for r in cur.fetchall()]

    def executescript(self, sql: str) -> None:
        self.conn.executescript(sql)
        self.conn.commit()


# --------------------------------------------------------------------------- #
# sync logic
# --------------------------------------------------------------------------- #

def init_schema(db) -> None:
    # Forward-compatible + idempotent: add the `state` column to a pre-existing
    # table BEFORE running SCHEMA (whose state index references the column).
    # On a fresh DB the ALTER no-ops (no table yet) and SCHEMA creates the table
    # with `state` already in it. Runs cheaply on every push.
    try:
        db.execute("ALTER TABLE tee_times ADD COLUMN state TEXT")
    except Exception:  # noqa: BLE001 — column already exists, or fresh DB
        pass
    db.executescript(SCHEMA)


def migrate(db, registry_path: str | None = None) -> dict:
    """Idempotent forward migration: ensure schema (incl. the state column and
    indexes) and backfill state on legacy rows from the registry. Safe to run
    repeatedly."""
    init_schema(db)
    backfilled = 0
    if registry_path:
        from collections import defaultdict
        import pathlib as _p
        reg = json.loads(_p.Path(registry_path).read_text())["courses"]
        by_state: dict[str, list[str]] = defaultdict(list)
        for c in reg:
            if c.get("state"):
                by_state[c["state"]].append(c["slug"])
        for st, slugs in by_state.items():
            for i in range(0, len(slugs), SLUG_CHUNK):
                batch = slugs[i:i + SLUG_CHUNK]
                ph = ",".join("?" * len(batch))
                db.execute(
                    f"UPDATE tee_times SET state=? WHERE course_slug IN ({ph}) "
                    "AND (state IS NULL OR state='')", [st, *batch])
                backfilled += len(batch)
    return {"backfilled_course_batches": backfilled}


def _key(t: dict) -> tuple[str, str]:
    return (t["course_slug"], t["teetime"])


def sync(db, doc: dict) -> dict:
    """Diff-sync one aggregate result document into the tee_times table."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    date = doc["date"]
    scraped = {}
    for t in doc["tee_times"]:
        scraped[_key(t)] = {
            "course_slug": t["course_slug"], "teetime": t["teetime"],
            "course_name": t["course_name"], "city": t.get("city"),
            "state": t.get("state"),
            "platform": t.get("platform"),
            "holes": "/".join(map(str, t.get("holes") or [])),
            "open_spots": t.get("open_spots"),
            "price_min": t.get("price_min"), "price_max": t.get("price_max"),
            "currency": t.get("currency", "USD"),
            "booking_url": t.get("booking_url"),
            "simulated": 1 if t.get("simulated") else 0,
            "active": 1, "first_seen_at": now, "last_seen_at": now,
        }

    # courses that errored this run must NOT have their rows deactivated
    errored = {e["course"] for e in doc.get("errors", [])}
    scraped_courses = {k[0] for k in scraped}

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
                "SELECT course_slug, teetime, open_spots, price_min, price_max, "
                f"active FROM tee_times WHERE substr(teetime,1,10) = ? "
                f"AND course_slug IN ({ph})", [date, *batch]):
            existing[(r["course_slug"], r["teetime"])] = r

    to_insert = [v for k, v in scraped.items() if k not in existing]
    to_update = []
    for k, v in scraped.items():
        e = existing.get(k)
        if e and (e["open_spots"] != v["open_spots"]
                  or e["price_min"] != v["price_min"]
                  or e["price_max"] != v["price_max"]
                  or not e["active"]):
            to_update.append(v)
    to_deactivate = [k for k, e in existing.items()
                     if e["active"] and k not in scraped
                     and k[0] in scraped_courses and k[0] not in errored]

    for i in range(0, len(to_insert), CHUNK):
        chunk = to_insert[i:i + CHUNK]
        placeholders = ",".join("(" + ",".join("?" * len(COLS)) + ")"
                                for _ in chunk)
        params = [row[c] for row in chunk for c in COLS]
        db.execute(f"INSERT OR REPLACE INTO tee_times ({','.join(COLS)}) "
                   f"VALUES {placeholders}", params)

    for row in to_update:
        db.execute(
            "UPDATE tee_times SET open_spots=?, price_min=?, price_max=?, "
            "active=1, last_seen_at=? WHERE course_slug=? AND teetime=?",
            [row["open_spots"], row["price_min"], row["price_max"],
             now, row["course_slug"], row["teetime"]])

    for slug, teetime in to_deactivate:
        db.execute("UPDATE tee_times SET active=0, last_seen_at=? "
                   "WHERE course_slug=? AND teetime=?", [now, slug, teetime])

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
# CLI
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="Cloudflare D1 tee-time store")
    p.add_argument("cmd", choices=["init", "migrate", "push", "stats"])
    p.add_argument("--data", default="output/tee_times.json")
    p.add_argument("--registry", default="registry.json")
    p.add_argument("--local", metavar="SQLITE_FILE",
                   help="use a local SQLite file instead of Cloudflare D1")
    a = p.parse_args()

    db = SqliteLocal(a.local) if a.local else D1Rest()

    if a.cmd == "init":
        init_schema(db)
        print("schema ensured")
    elif a.cmd == "migrate":
        s = migrate(db, a.registry)
        print(f"migrated: state column ensured, indexes created, "
              f"backfill touched {s['backfilled_course_batches']} courses")
    elif a.cmd == "push":
        init_schema(db)
        doc = json.loads(pathlib.Path(a.data).read_text())
        s = sync(db, doc)
        print(f"synced {a.data} for {doc['date']}: "
              f"+{s['rows_inserted']} inserted, ~{s['rows_updated']} updated, "
              f"-{s['rows_deactivated']} deactivated "
              f"(total writes ≈ {sum(s.values()) + 1})")
    elif a.cmd == "stats":
        total = db.execute("SELECT COUNT(*) AS n, SUM(active) AS act FROM tee_times")
        runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5")
        print("tee_times rows:", total[0])
        for r in runs:
            print(" run:", dict(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
