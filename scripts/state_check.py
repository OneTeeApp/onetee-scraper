"""Verify the state column: row counts by state (incl NULL) + active courses."""
import sys; sys.path.insert(0, ".")
from scraper.d1 import D1Rest
db = D1Rest()
rows = db.execute("SELECT COALESCE(state,'(null)') AS s, COUNT(*) AS n, "
                  "SUM(active) AS act FROM tee_times GROUP BY state ORDER BY n DESC")
print("tee_times by state:")
for r in rows: print(f"  {r['s']:<8} rows={r['n']:<7} active={r['act']}")
idx = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_teetimes%'")
print("indexes:", [r["name"] for r in idx])
