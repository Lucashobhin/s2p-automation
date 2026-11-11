"""Shared test helpers for the check_* scripts (not for production code)."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import delete, func, select

from app.db.database import SessionLocal
from app.models import (
    QCS, PurchaseOrder, PurchaseRequisition, Quotation, RFQ, RFQVendor,
)
from app.services.pricing import compute_quotation_amounts


def new_pr(session, tag: str, quantity: int = 10) -> PurchaseRequisition:
    pr = PurchaseRequisition(
        requester=tag, department="Procurement", item_id="I001", quantity=quantity,
        estimated_budget=D("100000.00"), required_delivery_date=date(2026, 12, 31),
    )
    session.add(pr)
    session.flush()
    return pr


def new_quote(rfq_id, vendor_id, qty=10, price=D("4200.00"), tax=D("18.00"), **kw) -> Quotation:
    amt = compute_quotation_amounts(qty, price, tax)
    data = dict(
        rfq_id=rfq_id, vendor_id=vendor_id, quoted_quantity=qty, unit_price=price,
        tax_rate_percent=tax, net_amount=amt.net_amount, tax_amount=amt.tax_amount,
        total_amount=amt.total_amount, lead_time_days=14,
    )
    data.update(kw)
    return Quotation(**data)


def new_po(pr_id, quotation_id, vendor_id, quantity=10, **kw) -> PurchaseOrder:
    data = dict(
        pr_id=pr_id, quotation_id=quotation_id, vendor_id=vendor_id, quantity=quantity,
        agreed_amount=D("49560.00"), delivery_date=date(2026, 12, 20),
    )
    data.update(kw)
    return PurchaseOrder(**data)


def cleanup(tag: str) -> None:
    """Delete everything hanging off PRs created with this tag (FK-safe order)."""
    with SessionLocal() as s:
        pr_ids = list(s.scalars(select(PurchaseRequisition.id).where(PurchaseRequisition.requester == tag)))
        rfq_ids = list(s.scalars(select(RFQ.id).where(RFQ.pr_id.in_(pr_ids)))) if pr_ids else []
        if pr_ids:
            s.execute(delete(PurchaseOrder).where(PurchaseOrder.pr_id.in_(pr_ids)))
        if rfq_ids:
            s.execute(delete(QCS).where(QCS.rfq_id.in_(rfq_ids)))
            s.execute(delete(Quotation).where(Quotation.rfq_id.in_(rfq_ids)))
            s.execute(delete(RFQVendor).where(RFQVendor.rfq_id.in_(rfq_ids)))
            s.execute(delete(RFQ).where(RFQ.id.in_(rfq_ids)))
        if pr_ids:
            s.execute(delete(PurchaseRequisition).where(PurchaseRequisition.id.in_(pr_ids)))
        s.commit()


def total_rows() -> int:
    with SessionLocal() as s:
        return sum(
            s.scalar(select(func.count()).select_from(m))
            for m in (PurchaseRequisition, RFQ, RFQVendor, Quotation, QCS, PurchaseOrder)
        )
