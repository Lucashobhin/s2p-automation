from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Integer,
    Numeric, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import POStatus
from app.db.base import Base
from app.models.purchase_requisition import utcnow


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        # PO vendor must be the vendor who made the quotation.
        ForeignKeyConstraint(
            ["quotation_id", "vendor_id"],
            ["quotations.id", "quotations.vendor_id"],
            name="fk_po_quotation_vendor",
        ),
        CheckConstraint("quantity > 0", name="ck_po_qty_positive"),
        CheckConstraint("agreed_amount >= 0", name="ck_po_amount_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # B-lite: several POs per PR are allowed. Over-ordering is blocked by
    # purchase_requisitions.ordered_quantity (see app/services/po_quantity.py).
    pr_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_requisitions.id", name="fk_po_pr"), nullable=False, index=True
    )
    # D6: one PO per quotation.
    quotation_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    vendor_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    agreed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # tax included
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=POStatus.PENDING_APPROVAL.value, index=True
    )
    erp_reference: Mapped[str | None] = mapped_column(String(50), unique=True)  # filled by mock SAP
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def po_number(self) -> str:
        return f"PO-{self.id:05d}"
