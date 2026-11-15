"""quotations: revisions (one ACTIVE per RFQ and vendor), request hash, idempotency scoped per vendor

Revision ID: 5d3e0a8b2c44
Revises: 4c2d9f7a1b33
"""
import sqlalchemy as sa
from alembic import op

revision = "5d3e0a8b2c44"
down_revision = "4c2d9f7a1b33"
branch_labels = None
depends_on = None

# SQLite stored the old UNIQUE on idempotency_key WITHOUT a name; this convention names it while
# Alembic rebuilds the table, so it can be dropped.
_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_ACTIVE = "status = 'ACTIVE'"


def upgrade() -> None:
    # Old rows would all become revision 1 / ACTIVE, which may be false. Quotations did not exist before 3d.
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM quotations")).scalar():
        raise RuntimeError("quotations already has rows; delete test quotations first.")
    with op.batch_alter_table("quotations", naming_convention=_NAMING) as batch_op:
        batch_op.drop_constraint("uq_quotations_idempotency_key", type_="unique")
        batch_op.drop_constraint("uq_quotation_rfq_vendor", type_="unique")
        batch_op.add_column(sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
        batch_op.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint("ck_quote_revision_positive", "revision >= 1")
        batch_op.create_unique_constraint("uq_quotation_rfq_vendor_revision", ["rfq_id", "vendor_id", "revision"])
        batch_op.create_unique_constraint("uq_quotation_rfq_vendor_key", ["rfq_id", "vendor_id", "idempotency_key"])
        batch_op.create_index(
            "uq_quotation_one_active", ["rfq_id", "vendor_id"], unique=True,
            sqlite_where=sa.text(_ACTIVE), postgresql_where=sa.text(_ACTIVE),
        )


def downgrade() -> None:
    # Fails if a vendor has several revisions or two vendors share a key (that is what upgrade made legal).
    with op.batch_alter_table("quotations") as batch_op:
        batch_op.drop_index("uq_quotation_one_active", sqlite_where=sa.text(_ACTIVE), postgresql_where=sa.text(_ACTIVE))
        batch_op.drop_constraint("uq_quotation_rfq_vendor_key", type_="unique")
        batch_op.drop_constraint("uq_quotation_rfq_vendor_revision", type_="unique")
        batch_op.drop_constraint("ck_quote_revision_positive", type_="check")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("revision")
        batch_op.create_unique_constraint("uq_quotation_rfq_vendor", ["rfq_id", "vendor_id"])
        batch_op.create_unique_constraint("uq_quotations_idempotency_key", ["idempotency_key"])