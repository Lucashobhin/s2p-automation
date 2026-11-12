from datetime import datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ApprovalDecision
from app.db.base import Base
from app.models.purchase_requisition import utcnow

_DECISIONS = ", ".join(f"'{d.value}'" for d in ApprovalDecision)


class Approval(Base):
    """One decision by one approver on one PR, QCS or PO. Rows are history: never edited."""

    __tablename__ = "approvals"
    __table_args__ = (
        # Exactly one of pr_id / qcs_id / po_id is filled (real foreign keys, D9).
        CheckConstraint(
            "(CASE WHEN pr_id IS NOT NULL THEN 1 ELSE 0 END)"
            " + (CASE WHEN qcs_id IS NOT NULL THEN 1 ELSE 0 END)"
            " + (CASE WHEN po_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_approval_exactly_one_entity",
        ),
        CheckConstraint(f"decision IN ({_DECISIONS})", name="ck_approval_decision_valid"),
        # The docs only let a manager "return" a PR.
        CheckConstraint("decision <> 'RETURNED' OR pr_id IS NOT NULL", name="ck_approval_returned_only_pr"),
        CheckConstraint("level >= 1", name="ck_approval_level_positive"),
        CheckConstraint("approval_cycle >= 1", name="ck_approval_cycle_positive"),
        # D11 option A: ONE decision per entity, level and cycle. SQLite/PostgreSQL treat NULLs as
        # distinct, so a QCS row can never collide with a PR row that has the same numbers.
        UniqueConstraint("pr_id", "level", "approval_cycle", name="uq_approval_pr_level_cycle"),
        UniqueConstraint("qcs_id", "level", "approval_cycle", name="uq_approval_qcs_level_cycle"),
        UniqueConstraint("po_id", "level", "approval_cycle", name="uq_approval_po_level_cycle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # RESTRICT: an approved record can never be deleted out from under its approval history.
    pr_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_requisitions.id", name="fk_approval_pr", ondelete="RESTRICT")
    )
    qcs_id: Mapped[int | None] = mapped_column(
        ForeignKey("qcs.id", name="fk_approval_qcs", ondelete="RESTRICT")
    )
    po_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_orders.id", name="fk_approval_po", ondelete="RESTRICT")
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 1 + number of RETURNED decisions so far for this entity. Derived by the service, not stored on the PR.
    approval_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approver: Mapped[str] = mapped_column(String(100), nullable=False)  # free text until login (A14)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    comments: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)