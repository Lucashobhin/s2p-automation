import sqlite3
from pathlib import Path

db = Path("s2p_poc.db").resolve()
print("DB file:", db, "| exists:", db.exists())

conn = sqlite3.connect(db)
tables = [r[0] for r in conn.execute(
    "select name from sqlite_master where type='table' order by name"
)]
print("Tables:", tables)

for t in tables:
    if t == "alembic_version":
        print("alembic_version:", list(conn.execute("select * from alembic_version")))
        continue
    print(f"\n{t} columns:")
    for cid, name, ctype, notnull, default, pk in conn.execute(f"pragma table_info({t})"):
        print(f"  {name:24} {ctype:14} {'NOT NULL' if notnull else 'null':9} {'PK' if pk else ''}")
conn.close()
