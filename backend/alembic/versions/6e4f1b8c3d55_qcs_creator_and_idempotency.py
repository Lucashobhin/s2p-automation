"""qcs: creator, create idempotency key and request hash (Slice 3f: D48, D55)

Revision ID: 6e4f1b8c3d55
Revises: 5d3e0a8b2c44
"""
import sqlalchemy as sa
from alembic import op

revision = "6e4f1b8c3d55"
down_revision = "5d3e0a8b2c44"
branch_labels = None
depends_on = None

# No naming_convention here: every existing constraint on qcs already has a name
# (uq_qcs_rfq, fk_qcs_rfq_link, fk_qcs_quotation_rfq), so batch mode can carry them over.
# The unique constraint makes SQLite REBUILD the table; approvals.qcs_id points at it, so the
# sandbox check (scripts/check_qcs_migration.py) verifies that foreign key survives.


def upgrade() -> None:
    # The new columns are nullable, but old rows would have no creator/key. qcs did not exist as a
    # workflow before 3f, so refuse instead of guessing.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM qcs")).scalar():
        raise RuntimeError("qcs already has rows; delete test QCS rows first.")
    with op.batch_alter_table("qcs") as batch_op:
        batch_op.add_column(sa.Column("created_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_qcs_rfq_key", ["rfq_id", "idempotency_key"])


def downgrade() -> None:
    # Drops the three columns: any creator names or keys stored in them are lost.
    with op.batch_alter_table("qcs") as batch_op:
        batch_op.drop_constraint("uq_qcs_rfq_key", type_="unique")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("created_by")