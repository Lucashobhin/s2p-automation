from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import RFQStatus
from app.db.base import Base
from app.models.purchase_requisition import utcnow


class RFQ(Base):
    __tablename__ = "rfqs"
    __table_args__ = (
        # One PR may have several tender rounds, but each round number only once.
        UniqueConstraint("pr_id", "round_number", name="uq_rfq_pr_round"),
        CheckConstraint("round_number >= 1", name="ck_rfq_round_positive"),
        CheckConstraint(
            "response_deadline >= issue_date", name="ck_rfq_deadline_after_issue"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pr_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_requisitions.id", name="fk_rfq_pr"), nullable=False, index=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    issue_date: Mapped[date | None] = mapped_column(Date)          # set when published
    response_deadline: Mapped[date | None] = mapped_column(Date)   # set when published
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RFQStatus.DRAFT.value, index=True
    )
    # Same key = same request retried (return the existing RFQ).
    # New round_number = a deliberate new tender. Two different concerns.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def rfq_number(self) -> str:
        return f"RFQ-{self.id:05d}"


class RFQVendor(Base):
    """Which vendors were invited to an RFQ. The pair is the primary key = unique."""

    __tablename__ = "rfq_vendors"

    rfq_id: Mapped[int] = mapped_column(
        ForeignKey("rfqs.id", name="fk_rfqvendor_rfq"), primary_key=True
    )
    vendor_id: Mapped[str] = mapped_column(
        ForeignKey("vendors.vendor_id", name="fk_rfqvendor_vendor"), primary_key=True, index=True
    )
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)