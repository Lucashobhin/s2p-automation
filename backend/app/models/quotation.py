from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, Numeric,
    String, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import QuotationStatus
from app.db.base import Base
from app.models.purchase_requisition import utcnow

_ACTIVE_SQL = f"status = '{QuotationStatus.ACTIVE.value}'"


class Quotation(Base):
    __tablename__ = "quotations"
    __table_args__ = (
        # Only an INVITED vendor can quote: (rfq_id, vendor_id) must exist in rfq_vendors.
        ForeignKeyConstraint(
            ["rfq_id", "vendor_id"],
            ["rfq_vendors.rfq_id", "rfq_vendors.vendor_id"],
            name="fk_quotation_invited_vendor",
        ),
        # D38: a vendor may REVISE its quotation until the deadline. Every revision is kept; the database
        # guarantees only one is ACTIVE per RFQ and vendor (a partial unique index), and revision numbers
        # are never reused.
        Index(
            "uq_quotation_one_active", "rfq_id", "vendor_id", unique=True,
            sqlite_where=text(_ACTIVE_SQL), postgresql_where=text(_ACTIVE_SQL),
        ),
        UniqueConstraint("rfq_id", "vendor_id", "revision", name="uq_quotation_rfq_vendor_revision"),
        # Idempotency keys are scoped to the RFQ AND the vendor: two vendors may both use "QUOTE-001".
        UniqueConstraint("rfq_id", "vendor_id", "idempotency_key", name="uq_quotation_rfq_vendor_key"),
        # Targets for composite foreign keys from QCS and PO (keep QCS/PO consistent).
        UniqueConstraint("rfq_id", "id", name="uq_quotation_rfq_id"),
        UniqueConstraint("id", "vendor_id", name="uq_quotation_id_vendor"),
        CheckConstraint("quoted_quantity > 0", name="ck_quote_qty_positive"),
        CheckConstraint("unit_price >= 0", name="ck_quote_unit_price_nonneg"),
        CheckConstraint("tax_rate_percent >= 0 AND tax_rate_percent <= 100", name="ck_quote_tax_rate_range"),
        CheckConstraint("net_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0", name="ck_quote_amounts_nonneg"),
        CheckConstraint("lead_time_days >= 0", name="ck_quote_lead_time_nonneg"),
        CheckConstraint("revision >= 1", name="ck_quote_revision_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 1 for the first submission by this vendor on this RFQ, then 2, 3, ... for each revision.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
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
        String(20), nullable=False, default=QuotationStatus.ACTIVE.value
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    # sha256 of the normalised submission: tells a retry or a no-change resubmission from a real revision.
    request_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )