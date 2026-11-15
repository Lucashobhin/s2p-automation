"""Vendor quotations (Phase 3d): submit, revise until the deadline, read.

Every write is ONE transaction: quotation row(s) + audit row (D14). The server computes the money.
A vendor's earlier quotations are never deleted: a revision marks the previous ACTIVE one SUPERSEDED, and the
database allows only one ACTIVE quotation per RFQ and vendor (partial unique index).

Order of checks for a submission (it matters):
  1. a known Idempotency-Key -> return what that request produced (works at any time, even after the deadline)
  2. RFQ exists, is PUBLISHED, and the deadline has not passed (revisions are NOT allowed after it)
  3. the vendor was invited
  4. identical to the vendor's current ACTIVE quotation -> return it, create nothing
  5. quantity / currency / amount rules, then insert (superseding the previous ACTIVE one)
Every "lost a race" path re-runs checks 1 and 4 first: the winner may be our own twin.
"""
import hashlib
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import rules
from app.core.enums import QuotationStatus, RFQStatus
from app.models.purchase_requisition import PurchaseRequisition
from app.models.quotation import Quotation
from app.models.rfq import RFQ, RFQVendor
from app.schemas.quotation import QuotationSubmit
from app.services.audit import record_audit
from app.services.pricing import compute_quotation_amounts
from app.services.rfqs import RFQConflict, RFQError, RFQInvalid, RFQNotFound

MAX_TOTAL = Decimal("1000000000000")   # Numeric(14, 2) holds totals below 10^12


class RFQForbidden(RFQError):
    status_code = 403


class _LostRace(Exception):
    pass


def request_hash(p: QuotationSubmit) -> str:
    """Hash of the NORMALISED submission (every field the vendor sends, no idempotency key)."""
    canonical = {
        "vendor_id": p.vendor_id,
        "quoted_quantity": p.quoted_quantity,
        "unit_price": format(p.unit_price.normalize(), "f"),
        "tax_rate_percent": format(p.tax_rate_percent.normalize(), "f"),
        "currency": p.currency,
        "lead_time_days": p.lead_time_days,
        "payment_terms": p.payment_terms,
        "source_document_ref": p.source_document_ref,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _find_by_key(session: Session, rfq_id: int, vendor_id: str, key: str) -> Quotation | None:
    # Scoped to RFQ AND vendor: two vendors may both use "QUOTE-001".
    return session.scalars(select(Quotation).where(
        Quotation.rfq_id == rfq_id, Quotation.vendor_id == vendor_id, Quotation.idempotency_key == key)).first()


def _active(session: Session, rfq_id: int, vendor_id: str) -> Quotation | None:
    return session.scalars(select(Quotation).where(
        Quotation.rfq_id == rfq_id, Quotation.vendor_id == vendor_id,
        Quotation.status == QuotationStatus.ACTIVE.value)).first()


def _replay(existing: Quotation, digest: str) -> tuple[Quotation, bool]:
    # A late retry of an OLD revision returns that revision (status SUPERSEDED): it never reverts a newer one.
    if existing.request_hash == digest:
        return existing, False
    raise RFQConflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was already used with a different quotation")


def _snapshot(q: Quotation) -> dict:
    return {
        "id": q.id, "rfq_id": q.rfq_id, "vendor_id": q.vendor_id, "revision": q.revision, "status": q.status,
        "quoted_quantity": q.quoted_quantity, "unit_price": str(q.unit_price),
        "tax_rate_percent": str(q.tax_rate_percent), "net_amount": str(q.net_amount),
        "tax_amount": str(q.tax_amount), "total_amount": str(q.total_amount), "currency": q.currency,
        "lead_time_days": q.lead_time_days, "payment_terms": q.payment_terms,
    }


def check_not_sealed(rfq: RFQ, today: date) -> None:
    """Sealed bids (off by default). When on, EVERYONE is refused until the deadline has passed or the RFQ is
    no longer PUBLISHED. The API has no authentication, so it cannot tell the buyer from a competitor."""
    if (rules.SEAL_QUOTATIONS_UNTIL_DEADLINE and rfq.status == RFQStatus.PUBLISHED.value
            and rfq.response_deadline is not None and today <= rfq.response_deadline):
        raise RFQForbidden(
            "QUOTATIONS_SEALED",
            "Quotations are sealed until the response deadline has passed; this applies to everyone "
            "(there is no login to tell a buyer from a competitor)",
            response_deadline=rfq.response_deadline.isoformat(),
        )


def submit_quotation(
    session: Session, rfq_id: int, payload: QuotationSubmit, *,
    idempotency_key: str | None = None, correlation_id: str | None = None, today: date | None = None,
) -> tuple[Quotation, bool]:
    """Returns (quotation, created). created=False: an idempotent replay or a no-change resubmission."""
    today = today or date.today()
    digest = request_hash(payload)
    vendor_id = payload.vendor_id

    if idempotency_key is not None:                                            # 1
        existing = _find_by_key(session, rfq_id, vendor_id, idempotency_key)
        if existing is not None:
            return _replay(existing, digest)

    rfq = session.get(RFQ, rfq_id)                                             # 2
    if rfq is None:
        raise RFQNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    if rfq.status != RFQStatus.PUBLISHED.value:
        raise RFQConflict("RFQ_NOT_ACCEPTING", f"RFQ is {rfq.status}; quotations are only accepted while it is PUBLISHED",
                          rfq_status=rfq.status)
    if today > rfq.response_deadline:
        raise RFQConflict("RFQ_DEADLINE_PASSED", "The response deadline has passed; no new quotations or revisions",
                          response_deadline=rfq.response_deadline.isoformat())

    invited = session.get(RFQVendor, (rfq_id, vendor_id))                      # 3
    if invited is None:
        raise RFQForbidden("NOT_INVITED", f"Vendor {vendor_id!r} was not invited to this RFQ")

    current = _active(session, rfq_id, vendor_id)                              # 4
    if current is not None and current.request_hash == digest:
        return current, False

    if payload.quoted_quantity > rfq.requested_quantity:                       # 5
        raise RFQInvalid("QUANTITY_EXCEEDS_REQUEST", "quoted_quantity is more than the RFQ asked for",
                         requested_quantity=rfq.requested_quantity, quoted_quantity=payload.quoted_quantity)
    pr = session.get(PurchaseRequisition, rfq.pr_id)
    if payload.currency != pr.currency:
        raise RFQInvalid("CURRENCY_MISMATCH", "The quotation currency must equal the PR's currency",
                         pr_currency=pr.currency, quoted_currency=payload.currency)
    amounts = compute_quotation_amounts(payload.quoted_quantity, payload.unit_price, payload.tax_rate_percent)
    if amounts.total_amount >= MAX_TOTAL:
        raise RFQInvalid("AMOUNT_TOO_LARGE", "unit_price x quoted_quantity (plus tax) is too large to store",
                         limit=str(MAX_TOTAL))

    revision = 1 + (session.scalar(select(func.max(Quotation.revision)).where(
        Quotation.rfq_id == rfq_id, Quotation.vendor_id == vendor_id)) or 0)
    before = _snapshot(current) if current is not None else None
    quotation = Quotation(
        rfq_id=rfq_id, vendor_id=vendor_id, revision=revision, status=QuotationStatus.ACTIVE.value,
        quoted_quantity=payload.quoted_quantity, unit_price=payload.unit_price,
        tax_rate_percent=payload.tax_rate_percent, net_amount=amounts.net_amount,
        tax_amount=amounts.tax_amount, total_amount=amounts.total_amount, currency=pr.currency,
        lead_time_days=payload.lead_time_days, payment_terms=payload.payment_terms,
        source_document_ref=payload.source_document_ref, idempotency_key=idempotency_key, request_hash=digest,
    )
    try:
        if current is not None:   # supersede FIRST: the partial unique index allows one ACTIVE row at a time
            result = session.execute(
                update(Quotation).where(Quotation.id == current.id, Quotation.status == QuotationStatus.ACTIVE.value)
                .values(status=QuotationStatus.SUPERSEDED.value))
            if result.rowcount != 1:
                raise _LostRace()
        session.add(quotation)
        session.flush()
        record_audit(
            session, actor=vendor_id,
            action="QUOTATION_SUBMITTED" if revision == 1 else "QUOTATION_REVISED",
            entity_type="QUOTATION", entity_id=quotation.id, before=before, after=_snapshot(quotation),
            correlation_id=correlation_id,
        )
        session.commit()
    except (IntegrityError, _LostRace):
        session.rollback()
        if idempotency_key is not None:                    # the winner may be our own twin: re-run checks 1 and 4
            existing = _find_by_key(session, rfq_id, vendor_id, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        now = _active(session, rfq_id, vendor_id)
        if now is not None and now.request_hash == digest:
            return now, False
        raise RFQConflict("QUOTATION_CHANGED",
                          "Another submission by this vendor was recorded first; read the current quotation and decide again")
    except Exception:
        session.rollback()
        raise
    return quotation, True


def list_quotations(
    session: Session, rfq_id: int, *, include_history: bool = False, today: date | None = None,
) -> list[Quotation]:
    today = today or date.today()
    rfq = session.get(RFQ, rfq_id)
    if rfq is None:
        raise RFQNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    check_not_sealed(rfq, today)
    stmt = select(Quotation).where(Quotation.rfq_id == rfq_id)
    if not include_history:
        stmt = stmt.where(Quotation.status == QuotationStatus.ACTIVE.value)
    return list(session.scalars(stmt.order_by(Quotation.vendor_id, Quotation.revision)))


def get_quotation(session: Session, quotation_id: int, *, today: date | None = None) -> Quotation:
    today = today or date.today()
    q = session.get(Quotation, quotation_id)
    if q is None:
        raise RFQNotFound("QUOTATION_NOT_FOUND", f"Quotation {quotation_id} not found")
    check_not_sealed(session.get(RFQ, q.rfq_id), today)
    return q