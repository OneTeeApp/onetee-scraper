"""One-time: delete D1 tee_times rows whose course_slug is no longer in the
registry (orphaned by course renames when adopting the AZ v2 directory).
Safe: only removes slugs absent from registry.json."""
from __future__ import annotations
import sys
sys.path.insert(0, ".")
from scraper.d1 import D1Rest
from scraper.aggregate import load_registry

def main():
    db = D1Rest()
    reg_slugs = {c["slug"] for c in load_registry("registry.json")}
    rows = db.execute("SELECT DISTINCT course_slug FROM tee_times")
    d1_slugs = {r["course_slug"] for r in rows}
    orphans = sorted(d1_slugs - reg_slugs)
    print(f"registry slugs: {len(reg_slugs)} | D1 slugs: {len(d1_slugs)} | "
          f"orphans: {len(orphans)}", flush=True)
    for s in orphans:
        n = db.execute("SELECT COUNT(*) AS c FROM tee_times WHERE course_slug=?", [s])
        cnt = n[0]["c"] if n else "?"
        db.execute("DELETE FROM tee_times WHERE course_slug=?", [s])
        print(f"  deleted {s} ({cnt} rows)", flush=True)
    print("done", flush=True)

if __name__ == "__main__":
    main()
