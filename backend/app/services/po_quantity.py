"""The ONLY place that changes purchase_requisitions.ordered_quantity (B-lite, A25).

ordered_quantity = units covered by ACTIVE purchase orders
                   (PENDING_APPROVAL, APPROVED, ERP_PUSHED).

Rules:
  * Reserve with ONE conditional UPDATE, so two simultaneous buyers cannot both win.
  * Reserve and create the PO in the SAME transaction (caller commits once).
  * A PO that becomes REJECTED or CANCELLED gives its units back, in the same transaction.
"""
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.enums import ACTIVE_PO_STATUSES, POStatus
from app.models.purchase_order import PurchaseOrder
from app.models.purchase_requisition import PurchaseRequisition

# Only these PO states may be deactivated. ERP_PUSHED needs an ERP reversal first (A30).
DEACTIVATABLE_PO_STATUSES = (POStatus.PENDING_APPROVAL.value, POStatus.APPROVED.value)
TARGET_INACTIVE_STATUSES = (POStatus.REJECTED.value, POStatus.CANCELLED.value)


class POQuantityError(Exception):
    pass


class PRNotFound(POQuantityError):
    pass


class InsufficientRemainingQuantity(POQuantityError):
    def __init__(self, pr_id: int, requested: int, remaining: int):
        super().__init__(f"PR {pr_id}: requested {requested}, only {remaining} remaining")
        self.pr_id, self.requested, self.remaining = pr_id, requested, remaining


class POStateError(POQuantityError):
    pass


def reserve_po_quantity(session: Session, pr_id: int, quantity: int) -> None:
    """Claim `quantity` units of a PR. Call in the same transaction as the PO insert."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive int")

    result = session.execute(
        update(PurchaseRequisition)
        .where(
            PurchaseRequisition.id == pr_id,
            PurchaseRequisition.ordered_quantity + quantity <= PurchaseRequisition.quantity,
        )
        .values(ordered_quantity=PurchaseRequisition.ordered_quantity + quantity)
    )
    if result.rowcount == 1:
        return

    row = session.execute(
        select(PurchaseRequisition.quantity, PurchaseRequisition.ordered_quantity)
        .where(PurchaseRequisition.id == pr_id)
    ).first()
    if row is None:
        raise PRNotFound(f"PR {pr_id} not found")
    raise InsufficientRemainingQuantity(pr_id, quantity, row.quantity - row.ordered_quantity)


def release_po_quantity(session: Session, pr_id: int, quantity: int) -> None:
    """Give units back. Never lets the counter go below zero."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive int")

    result = session.execute(
        update(PurchaseRequisition)
        .where(
            PurchaseRequisition.id == pr_id,
            PurchaseRequisition.ordered_quantity - quantity >= 0,
        )
        .values(ordered_quantity=PurchaseRequisition.ordered_quantity - quantity)
    )
    if result.rowcount != 1:
        raise POQuantityError(f"cannot release {quantity} units on PR {pr_id}")


def deactivate_purchase_order(session: Session, po_id: int, new_status: str) -> None:
    """Move a PO to REJECTED or CANCELLED and release its units, atomically.

    The status flip is a conditional UPDATE, so cancelling twice cannot release twice.
    """
    if new_status not in TARGET_INACTIVE_STATUSES:
        raise ValueError(f"new_status must be one of {TARGET_INACTIVE_STATUSES}")

    result = session.execute(
        update(PurchaseOrder)
        .where(PurchaseOrder.id == po_id, PurchaseOrder.status.in_(DEACTIVATABLE_PO_STATUSES))
        .values(status=new_status)
    )
    if result.rowcount != 1:
        current = session.execute(
            select(PurchaseOrder.status).where(PurchaseOrder.id == po_id)
        ).scalar_one_or_none()
        if current is None:
            raise POStateError(f"PO {po_id} not found")
        raise POStateError(f"PO {po_id} is {current}; only {DEACTIVATABLE_PO_STATUSES} can be deactivated")

    row = session.execute(
        select(PurchaseOrder.pr_id, PurchaseOrder.quantity).where(PurchaseOrder.id == po_id)
    ).one()
    release_po_quantity(session, row.pr_id, row.quantity)


def find_quantity_mismatches(session: Session, pr_ids: list[int] | None = None):
    """Drift detector: PRs whose counter differs from the real sum of active PO quantities."""
    actual = (
        select(func.coalesce(func.sum(PurchaseOrder.quantity), 0))
        .where(
            PurchaseOrder.pr_id == PurchaseRequisition.id,
            PurchaseOrder.status.in_(ACTIVE_PO_STATUSES),
        )
        .correlate(PurchaseRequisition)
        .scalar_subquery()
    )
    stmt = select(
        PurchaseRequisition.id.label("pr_id"),
        PurchaseRequisition.ordered_quantity.label("counter"),
        actual.label("actual"),
    ).where(PurchaseRequisition.ordered_quantity != actual)
    if pr_ids is not None:
        stmt = stmt.where(PurchaseRequisition.id.in_(pr_ids))
    return session.execute(stmt).all()
