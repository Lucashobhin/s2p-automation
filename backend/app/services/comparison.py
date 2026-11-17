"""RFQ comparison (Phase 3e): the facts side by side. READ-ONLY and free of judgement.

It never writes, never scores, never ranks and never names a winner (scoring is Phase 5, the AI layer Phase 6).
Only each vendor's current ACTIVE quotation is shown. Rows are ordered by vendor id so that the order carries
no meaning. The like-for-like price is the tax-inclusive price per unit; totals are only compared when every
vendor quoted the same quantity.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import QuotationStatus
from app.models.item import Item
from app.models.purchase_requisition import PurchaseRequisition
from app.models.quotation import Quotation
from app.models.rfq import RFQ
from app.models.vendor import Vendor
from app.schemas.comparison import ComparisonRead, ComparisonRow
from app.services.quotations import check_not_sealed
from app.services.rfqs import RFQNotFound

PERCENT = Decimal("0.01")
UNIT = Decimal("0.0001")   # 4 decimals: rounding to 2 could make two different quotes look tied


def build_comparison(session: Session, rfq_id: int, *, today: date | None = None) -> ComparisonRead:
    today = today or date.today()
    rfq = session.get(RFQ, rfq_id)
    if rfq is None:
        raise RFQNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    check_not_sealed(rfq, today)   # same switch and same rule as the quotation list

    pr = session.get(PurchaseRequisition, rfq.pr_id)
    item = session.get(Item, pr.item_id)
    found = session.execute(
        select(Quotation, Vendor)
        .join(Vendor, Vendor.vendor_id == Quotation.vendor_id)
        .where(Quotation.rfq_id == rfq_id, Quotation.status == QuotationStatus.ACTIVE.value)
        .order_by(Quotation.vendor_id)
    ).all()

    unit_incl = {q.id: (q.total_amount / q.quoted_quantity).quantize(UNIT, rounding=ROUND_HALF_UP) for q, _v in found}
    quantities = {q.quoted_quantity for q, _v in found}
    comparable = None if len(found) < 2 else (len(quantities) == 1)
    best_unit = min(unit_incl.values()) if found else None
    best_lead = min(q.lead_time_days for q, _v in found) if found else None
    best_total = min(q.total_amount for q, _v in found) if comparable else None

    rows = []
    for q, v in found:
        rows.append(ComparisonRow(
            vendor_id=v.vendor_id, vendor_name=v.vendor_name, oem_approved=v.oem_approved,
            risk_flags=[] if v.oem_approved else ["NON_OEM"],
            quotation_id=q.id, revision=q.revision, quoted_quantity=q.quoted_quantity,
            coverage_percent=(Decimal(q.quoted_quantity) * 100 / Decimal(rfq.requested_quantity)).quantize(
                PERCENT, rounding=ROUND_HALF_UP),
            full_coverage=q.quoted_quantity == rfq.requested_quantity,
            unit_price=q.unit_price, tax_rate_percent=q.tax_rate_percent, net_amount=q.net_amount,
            tax_amount=q.tax_amount, total_amount=q.total_amount, unit_price_incl_tax=unit_incl[q.id],
            lead_time_days=q.lead_time_days, payment_terms=q.payment_terms,
            is_lowest_unit_price_incl_tax=unit_incl[q.id] == best_unit,
            is_shortest_lead_time=q.lead_time_days == best_lead,
            is_lowest_total=bool(comparable) and q.total_amount == best_total,
        ))
    note = None
    if comparable is False:
        note = ("Quoted quantities differ, so the totals are NOT directly comparable. "
                "Compare unit_price_incl_tax and look at coverage_percent.")
    return ComparisonRead(
        rfq_id=rfq.id, rfq_number=rfq.rfq_number, rfq_status=rfq.status, pr_id=pr.id, item_id=item.item_id,
        item_name=item.item_name, currency=pr.currency, requested_quantity=rfq.requested_quantity,
        response_deadline=rfq.response_deadline, quotation_count=len(rows), totals_comparable=comparable,
        totals_note=note, ordering="by vendor_id; the order carries no meaning", rows=rows,
    )