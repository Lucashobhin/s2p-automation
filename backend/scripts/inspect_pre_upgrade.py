"""Read-only: is s2p_poc.db clean before running the 3f migration? Changes nothing."""
import sqlite3

conn = sqlite3.connect("file:s2p_poc.db?mode=ro", uri=True)

print("leftover temp tables:", [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE name LIKE '%alembic_tmp%'")])
print("qcs columns:", [r[1] for r in conn.execute("PRAGMA table_info(qcs)")])
print("qcs rows:", conn.execute("SELECT COUNT(*) FROM qcs").fetchone()[0])
print("foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())

conn.close()
