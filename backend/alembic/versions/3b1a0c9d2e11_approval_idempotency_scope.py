"""approval idempotency key scoped per entity, plus request hash

Revision ID: 3b1a0c9d2e11
Revises: ade2de7eeea3
"""
import sqlalchemy as sa
from alembic import op

revision = "3b1a0c9d2e11"
down_revision = "ade2de7eeea3"
branch_labels = None
depends_on = None

# SQLite stored the old UNIQUE on idempotency_key WITHOUT a name, so it cannot be dropped by name.
# This naming convention makes Alembic name it while it rebuilds the table.
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("approvals", naming_convention=_NAMING) as batch_op:
        batch_op.drop_constraint("uq_approvals_idempotency_key", type_="unique")
        batch_op.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_approval_pr_key", ["pr_id", "idempotency_key"])
        batch_op.create_unique_constraint("uq_approval_qcs_key", ["qcs_id", "idempotency_key"])
        batch_op.create_unique_constraint("uq_approval_po_key", ["po_id", "idempotency_key"])


def downgrade() -> None:
    # Fails if two entities now share one key (that is exactly what upgrade made legal).
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint("uq_approval_po_key", type_="unique")
        batch_op.drop_constraint("uq_approval_qcs_key", type_="unique")
        batch_op.drop_constraint("uq_approval_pr_key", type_="unique")
        batch_op.drop_column("request_hash")
        batch_op.create_unique_constraint("uq_approvals_idempotency_key", ["idempotency_key"])