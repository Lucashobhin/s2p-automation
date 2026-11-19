"""Sandbox check for migration 6e4f1b8c3d55. Runs on COPIES of s2p_poc.db; the real DB is never changed.

Run from backend/:  python -m scripts.check_qcs_migration
"""
import hashlib
import importlib.util
import pathlib
import shutil
import sqlite3
import tempfile

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REAL = pathlib.Path("s2p_poc.db")
if not REAL.exists():
    raise SystemExit("s2p_poc.db not found (run from backend/)")
found = list(pathlib.Path("alembic/versions").glob("6e4f1b8c3d55_*.py"))
if len(found) != 1:
    raise SystemExit(f"expected exactly one migration file 6e4f1b8c3d55_*.py, found {len(found)}")
spec = importlib.util.spec_from_file_location("mig_6e4f", found[0])
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)

passed = failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label:<78} {extra}")


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_migration(db: pathlib.Path, direction: str) -> None:
    eng = sa.create_engine(f"sqlite:///{db.as_posix()}")

    @sa.event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):   # strict mode: foreign keys enforced during the rebuild
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    try:
        with eng.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                getattr(mig, direction)()
    finally:
        eng.dispose()


def raw(db: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def table_sql(conn, name: str) -> str:
    return conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]


def columns(conn, table: str) -> dict:
    return {r[1]: r[3] for r in conn.execute(f"PRAGMA table_info({table})")}   # name -> notnull flag


def unique_sets(conn, table: str) -> set:
    out = set()
    for _seq, name, unique, *_ in conn.execute(f"PRAGMA index_list({table})"):
        if unique:
            out.add(tuple(r[2] for r in conn.execute(f"PRAGMA index_info({name})")))
    return out


def counts(conn) -> tuple:
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("approvals", "quotations", "rfqs", "qcs"))


NEW_COLS = ("created_by", "idempotency_key", "request_hash")
TS = "2026-01-01 00:00:00"
real_hash_before = sha(REAL)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = pathlib.Path(tmp)

    print("--- upgrade on a copy")
    a = tmp / "a.db"
    shutil.copy(REAL, a)
    c = raw(a)
    check("start state: qcs has 0 rows", counts(c)[3] == 0)
    rfq_row = c.execute("SELECT id FROM rfqs LIMIT 1").fetchone()
    check("the DB has an RFQ to test with", rfq_row is not None)
    if rfq_row is None:
        raise SystemExit("need at least one RFQ row for the behaviour checks")
    rfq_id = rfq_row[0]
    approvals_sql_before = table_sql(c, "approvals")
    counts_before = counts(c)
    c.close()

    run_migration(a, "upgrade")
    c = raw(a)
    cols = columns(c, "qcs")
    check("three new columns exist", all(n in cols for n in NEW_COLS))
    check("...and are NULLABLE (notnull flag 0)", all(cols[n] == 0 for n in NEW_COLS if n in cols))
    qcs_sql = table_sql(c, "qcs")
    for name in ("uq_qcs_rfq", "uq_qcs_rfq_key", "fk_qcs_rfq_link", "fk_qcs_quotation_rfq"):
        check(f"qcs keeps/gets constraint {name}", name in qcs_sql)
    sets = unique_sets(c, "qcs")
    check("UNIQUE (rfq_id) still enforced (one QCS per RFQ)", ("rfq_id",) in sets)
    check("UNIQUE (rfq_id, idempotency_key) exists", ("rfq_id", "idempotency_key") in sets)
    check("index ix_qcs_status survived the rebuild",
          c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_qcs_status'").fetchone() is not None)
    check("PRAGMA foreign_key_check is empty", c.execute("PRAGMA foreign_key_check").fetchall() == [])
    check("PRAGMA integrity_check is ok", c.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
    check("approvals table SQL is byte-for-byte unchanged (FK to qcs intact)",
          table_sql(c, "approvals") == approvals_sql_before)
    check("approvals / quotations / rfqs row counts unchanged", counts(c)[:3] == counts_before[:3])

    print("--- behaviour after the rebuild")
    c.execute("INSERT INTO qcs (rfq_id, status, created_at, updated_at, created_by) VALUES (?, 'DRAFT', ?, ?, 'tester')",
              (rfq_id, TS, TS))
    c.commit()
    qcs_id = c.execute("SELECT id FROM qcs").fetchone()[0]
    try:
        c.execute("INSERT INTO qcs (rfq_id, status, created_at, updated_at) VALUES (?, 'DRAFT', ?, ?)", (rfq_id, TS, TS))
        check("a second QCS for the same RFQ is refused", False)
    except sqlite3.IntegrityError:
        check("a second QCS for the same RFQ is refused", True)
    c.rollback()
    c.execute("INSERT INTO approvals (qcs_id, level, approval_cycle, approver, decision, decided_at) "
              "VALUES (?, 1, 1, 'tester', 'APPROVED', ?)", (qcs_id, TS))
    c.commit()
    check("an approval row can point at the QCS", True)
    try:
        c.execute("DELETE FROM qcs WHERE id=?", (qcs_id,))
        check("deleting a QCS that has approvals is refused (RESTRICT)", False)
    except sqlite3.IntegrityError:
        check("deleting a QCS that has approvals is refused (RESTRICT)", True)
    c.rollback()
    try:
        c.execute("INSERT INTO approvals (qcs_id, level, approval_cycle, approver, decision, decided_at) "
                  "VALUES (999999, 1, 1, 'tester', 'APPROVED', ?)", (TS,))
        check("an approval for a non-existent QCS is refused (FK)", False)
    except sqlite3.IntegrityError:
        check("an approval for a non-existent QCS is refused (FK)", True)
    c.rollback()
    c.close()

    print("--- downgrade on a copy")
    b = tmp / "b.db"
    shutil.copy(REAL, b)
    run_migration(b, "upgrade")
    run_migration(b, "downgrade")
    c = raw(b)
    cols = columns(c, "qcs")
    check("downgrade removes the three columns", not any(n in cols for n in NEW_COLS))
    qcs_sql = table_sql(c, "qcs")
    check("downgrade removes uq_qcs_rfq_key and keeps the old constraints",
          "uq_qcs_rfq_key" not in qcs_sql and all(n in qcs_sql for n in ("uq_qcs_rfq", "fk_qcs_rfq_link", "fk_qcs_quotation_rfq")))
    check("downgrade keeps ix_qcs_status",
          c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_qcs_status'").fetchone() is not None)
    check("downgrade: foreign_key_check empty, approvals SQL unchanged",
          c.execute("PRAGMA foreign_key_check").fetchall() == [] and table_sql(c, "approvals") == approvals_sql_before)
    c.close()

    print("--- the guard (mutation of the start state)")
    g = tmp / "g.db"
    shutil.copy(REAL, g)
    c = raw(g)
    c.execute("INSERT INTO qcs (rfq_id, status, created_at, updated_at) VALUES (?, 'DRAFT', ?, ?)", (rfq_id, TS, TS))
    c.commit()
    c.close()
    try:
        run_migration(g, "upgrade")
        check("a QCS row present -> migration refuses to run", False)
    except RuntimeError as exc:
        check("a QCS row present -> migration refuses to run", "qcs already has rows" in str(exc))
    c = raw(g)
    check("...and the table was left untouched (no new columns)", not any(n in columns(c, "qcs") for n in NEW_COLS))
    c.close()

check("the real s2p_poc.db was NOT modified by this script", sha(REAL) == real_hash_before)
print(f"\n{passed}/{passed + failed} checks passed")
print("ALL PASS" if failed == 0 else "SOME FAILED")