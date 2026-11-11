from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import PRStatus
from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PurchaseRequisition(Base):
    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_pr_quantity_positive"),
        CheckConstraint("estimated_budget >= 0", name="ck_pr_budget_nonnegative"),
        # B-lite: units already covered by active POs can never exceed what was requested.
        CheckConstraint("ordered_quantity >= 0", name="ck_pr_ordered_nonneg"),
        CheckConstraint("ordered_quantity <= quantity", name="ck_pr_ordered_within_quantity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester: Mapped[str] = mapped_column(String(100), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Units covered by ACTIVE POs. Change ONLY via app/services/po_quantity.py.
    ordered_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    estimated_budget: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    required_delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PRStatus.SUBMITTED.value, index=True
    )
    # A repeated webhook with the same key must not create a second PR.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def pr_number(self) -> str:
        return f"PR-{self.id:05d}"
