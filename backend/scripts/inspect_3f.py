"""Read-only inspection for Slice 3f decisions D53, D55, D56. Changes nothing."""
import pathlib
import sqlite3

DB = pathlib.Path("s2p_poc.db").resolve()
if not DB.exists():
    raise SystemExit(f"DB not found: {DB} (run from backend/)")

conn = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)
cur = conn.cursor()


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


section("ALEMBIC HEAD")
print(cur.execute("SELECT version_num FROM alembic_version").fetchall())

section("ROW COUNTS")
for table in ("qcs", "quotations", "purchase_orders", "approvals"):
    n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"{table}: {n}")
try:
    n = cur.execute(
        "SELECT COUNT(*) FROM approvals WHERE qcs_id IS NOT NULL"
    ).fetchone()[0]
    print(f"approvals with qcs_id: {n}")
except sqlite3.Error as exc:
    print("approvals.qcs_id check failed:", exc)

section("CREATE SQL (status CHECKs, FKs, totals)")
for table in ("qcs", "quotations", "purchase_orders", "approvals"):
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    print(f"\n-- {table}")
    print(row[0] if row else "TABLE NOT FOUND")

section("INDEXES on qcs, purchase_orders, approvals")
for row in cur.execute(
    "SELECT name, tbl_name, sql FROM sqlite_master "
    "WHERE type='index' AND tbl_name IN ('qcs','purchase_orders','approvals')"
):
    print(row)

section("CODE LINES: total_amount / required_approval_levels")
app_dir = pathlib.Path("app")
needles = ("total_amount", "required_approval_levels")
for path in sorted(app_dir.rglob("*.py")):
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if any(n in line for n in needles):
            print(f"{path.as_posix()}:{lineno}: {line.strip()}")

conn.close()
print("\nDONE (read-only, nothing changed)")