from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric,
    String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import QuotationStatus
from app.db.base import Base
from app.models.purchase_requisition import utcnow


class Quotation(Base):
    __tablename__ = "quotations"
    __table_args__ = (
        # Only an INVITED vendor can quote: (rfq_id, vendor_id) must exist in rfq_vendors.
        ForeignKeyConstraint(
            ["rfq_id", "vendor_id"],
            ["rfq_vendors.rfq_id", "rfq_vendors.vendor_id"],
            name="fk_quotation_invited_vendor",
        ),
        # D3: one quotation per vendor per RFQ (revisions are out of scope).
        UniqueConstraint("rfq_id", "vendor_id", name="uq_quotation_rfq_vendor"),
        # Targets for composite foreign keys from QCS and PO (keep QCS/PO consistent).
        UniqueConstraint("rfq_id", "id", name="uq_quotation_rfq_id"),
        UniqueConstraint("id", "vendor_id", name="uq_quotation_id_vendor"),
        CheckConstraint("quoted_quantity > 0", name="ck_quote_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_quote_unit_price_nonneg"),
        CheckConstraint("tax_rate_percent >= 0 AND tax_rate_percent <= 100", name="ck_quote_tax_rate_range"),
        CheckConstraint("net_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0", name="ck_quote_amounts_nonneg"),
        CheckConstraint("lead_time_days >= 0", name="ck_quote_lead_time_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    quoted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Client supplies these two:
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)       # net, excl. tax
    tax_rate_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Server computes these (app/services/pricing.py), never trusted from the client:
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)    # tax included
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_terms: Mapped[str | None] = mapped_column(String(50))
    source_document_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=QuotationStatus.SUBMITTED.value
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )