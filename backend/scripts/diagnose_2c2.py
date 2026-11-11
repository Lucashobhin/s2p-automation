"""Read-only diagnosis of the 2c-ii tables. Changes nothing.

Run from backend/:  python -m scripts.diagnose_2c2
"""
import sqlite3
from pathlib import Path

DB = Path("s2p_poc.db").resolve()
conn = sqlite3.connect(DB)
print("DB:", DB)
print("SQLite version:", sqlite3.sqlite_version)

print("\n1. Leftover test rows from a failed check script (requester like 'check.%'):")
rows = conn.execute(
    "select id, requester, quantity, ordered_quantity from purchase_requisitions "
    "where requester like 'check.%'"
).fetchall()
print("  ", rows if rows else "none")

print("\n2. rfqs columns (name, type, NOT NULL?, default):")
for _cid, name, ctype, notnull, default, _pk in conn.execute("pragma table_info(rfqs)"):
    print(f"   {name:20} {ctype:10} {'NOT NULL' if notnull else 'null':9} default={default}")

print("\n3. quotations UNIQUE indexes (a PO/QCS foreign key needs these):")
for _seq, name, unique, _origin, _partial in conn.execute("pragma index_list(quotations)"):
    if unique:
        cols = [r[2] for r in conn.execute(f"pragma index_info('{name}')")]
        print(f"   {name:34} {cols}")

print("\n4. purchase_orders foreign keys:")
for row in conn.execute("pragma foreign_key_list(purchase_orders)"):
    print("  ", row[2], "from", row[3], "to", row[4])

print("\n5. Model source lines (what YOUR files say):")
for path, needles in (
    ("app/models/rfq.py", ("issue_date", "response_deadline")),
    ("app/models/quotation.py", ("UniqueConstraint(",)),
):
    print(f"   {path}")
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if any(n in line for n in needles):
            print(f"     {i:3}: {line.strip()[:110]}")
conn.close()
