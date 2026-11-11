"""Remove leftover rows from a check_* script that crashed before its own cleanup.

Run from backend/ ONLY while the rfqs and purchase_orders tables do not exist
(after `alembic downgrade`), so no other table can still point at these rows:
    python -m scripts.cleanup_test_data
"""
import sqlite3
from pathlib import Path

DB = Path("s2p_poc.db").resolve()
conn = sqlite3.connect(DB)

tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
blocking = sorted(tables & {"rfqs", "purchase_orders"})
if blocking:
    raise SystemExit(f"STOP: tables {blocking} still exist and may point at test rows. Downgrade first.")

before = conn.execute(
    "select count(*) from purchase_requisitions where requester like 'check.%'"
).fetchone()[0]
conn.execute("delete from purchase_requisitions where requester like 'check.%'")
conn.commit()
left = conn.execute("select count(*) from purchase_requisitions").fetchone()[0]
print(f"DB: {DB}")
print(f"deleted {before} leftover test PR row(s); purchase_requisitions now has {left} row(s)")
conn.close()