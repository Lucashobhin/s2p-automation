"""rfq: requested quantity, scoped idempotency, one open RFQ per PR

Revision ID: 4c2d9f7a1b33
Revises: 3b1a0c9d2e11
"""
import sqlalchemy as sa
from alembic import op

revision = "4c2d9f7a1b33"
down_revision = "3b1a0c9d2e11"
branch_labels = None
depends_on = None

# SQLite stored the old UNIQUE on idempotency_key WITHOUT a name; this convention names it while
# Alembic rebuilds the table, so it can be dropped.
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_OPEN = "status IN ('DRAFT', 'PUBLISHED', 'CLOSED')"


def upgrade() -> None:
    # requested_quantity is NOT NULL and has no sensible default for existing rows.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM rfqs")).scalar():
        raise RuntimeError("rfqs already has rows; delete test RFQs first (requested_quantity is required).")
    with op.batch_alter_table("rfqs", naming_convention=_NAMING) as batch_op:
        batch_op.drop_constraint("uq_rfqs_idempotency_key", type_="unique")
        batch_op.add_column(sa.Column("requested_quantity", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("idempotency_hash", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint("ck_rfq_requested_quantity_positive", "requested_quantity > 0")
        batch_op.create_unique_constraint("uq_rfq_pr_key", ["pr_id", "idempotency_key"])
        batch_op.create_index(
            "uq_rfq_one_open_per_pr", ["pr_id"], unique=True,
            sqlite_where=sa.text(_OPEN), postgresql_where=sa.text(_OPEN),
        )


def downgrade() -> None:
    with op.batch_alter_table("rfqs") as batch_op:
        batch_op.drop_index(
            "uq_rfq_one_open_per_pr",
            sqlite_where=sa.text(_OPEN), postgresql_where=sa.text(_OPEN),
        )
        batch_op.drop_constraint("uq_rfq_pr_key", type_="unique")
        batch_op.drop_constraint("ck_rfq_requested_quantity_positive", type_="check")
        batch_op.drop_column("idempotency_hash")
        batch_op.drop_column("requested_quantity")
        batch_op.create_unique_constraint("uq_rfqs_idempotency_key", ["idempotency_key"])