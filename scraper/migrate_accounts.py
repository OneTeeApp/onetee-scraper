#!/usr/bin/env python3
"""One-shot: migrate the gate's account tables (users, billing_events, searches)
from Cloudflare D1 to the VPS Postgres, via the /exec endpoint.

Reads D1 with CLOUDFLARE_* creds (D1Rest); writes the VPS with ONETEE_API_URL +
ONETEE_INGEST_TOKEN (HttpBackend). Run once in GitHub Actions immediately before
the gate cutover, so paid tiers + saved searches carry over. Idempotent:
re-running upserts users, ignores duplicate billing_events / searches.

Placeholders use bare `?` (the VPS /exec translates them to $1..$n). users uses
an explicit ON CONFLICT (id) DO UPDATE (users is not in /exec's INSERT OR REPLACE
PK map, so the explicit form is required); billing_events uses INSERT OR IGNORE
(translated to ON CONFLICT DO NOTHING); searches preserves its D1 integer id into
the VPS BIGSERIAL column, then the sequence is bumped past the max so new inserts
don't collide.
"""
import sys

from scraper.d1 import D1Rest, HttpBackend


def _copy_table(src, dst, table, cols, insert_sql):
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}")
    ph = ", ".join(["?"] * len(cols))
    sql = insert_sql.format(cols=", ".join(cols), ph=ph)
    for r in rows:
        dst.execute(sql, [r.get(c) for c in cols])
    return len(rows)


def main() -> int:
    src = D1Rest()      # D1 (source of truth until cutover)
    dst = HttpBackend()  # VPS /exec

    # users — explicit upsert (id is the Clerk user id, TEXT PK).
    ucols = ["id", "email", "created_at", "last_seen_at", "tier",
             "stripe_customer_id", "stripe_subscription_id",
             "subscription_status", "current_period_end"]
    uset = ", ".join(f"{c}=EXCLUDED.{c}" for c in ucols if c != "id")
    n_users = _copy_table(
        src, dst, "users", ucols,
        "INSERT INTO users ({cols}) VALUES ({ph}) "
        "ON CONFLICT (id) DO UPDATE SET " + uset)
    print(f"users: {n_users} upserted")

    # billing_events — webhook dedup log; INSERT OR IGNORE -> ON CONFLICT DO NOTHING.
    n_ev = _copy_table(
        src, dst, "billing_events", ["id", "type", "received_at"],
        "INSERT OR IGNORE INTO billing_events ({cols}) VALUES ({ph})")
    print(f"billing_events: {n_ev} inserted")

    # searches — preserve the D1 id into the VPS BIGSERIAL column.
    scols = ["id", "user_id", "created_at", "label", "criteria", "results"]
    srows = src.execute(f"SELECT {', '.join(scols)} FROM searches ORDER BY id")
    sph = ", ".join(["?"] * len(scols))
    for r in srows:
        dst.execute(
            f"INSERT INTO searches ({', '.join(scols)}) VALUES ({sph}) "
            "ON CONFLICT (id) DO NOTHING",
            [r.get(c) for c in scols])
    print(f"searches: {len(srows)} inserted")

    # Advance the BIGSERIAL so the gate's next INSERT gets a fresh id, not a
    # collision with a migrated one. GREATEST(..., 1) keeps setval in-bounds
    # even if searches were empty.
    dst.execute(
        "SELECT setval(pg_get_serial_sequence('searches','id'), "
        "GREATEST((SELECT MAX(id) FROM searches), 1))")

    # Verify counts on the VPS side.
    for t in ("users", "billing_events", "searches"):
        n = dst.execute(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        print(f"VPS {t}: {n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
