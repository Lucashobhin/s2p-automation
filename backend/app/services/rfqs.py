"""RFQ lifecycle (Phase 3c): generate from an approved PR, publish to invited vendors, cancel.

Every operation is ONE transaction: RFQ change + audit row (D14). FastAPI only RECORDS the
invitations; actually notifying vendors is n8n's job (Phase 7).
Errors carry a machine-readable code and the current state, so a caller (n8n) can tell
"already done" from "genuinely refused" by reading the state, not by guessing from a 409.
The one-open-RFQ-per-PR rule is ALSO enforced by a partial unique index; a race surfaces as 409.
"""
import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import PRStatus, RFQStatus
from app.models.item import Item
from app.models.purchase_requisition import PurchaseRequisition
from app.models.quotation import Quotation
from app.models.rfq import OPEN_RFQ_STATUSES, RFQ, RFQVendor
from app.models.vendor import Vendor
from app.schemas.rfq import RFQCancel, RFQPublish, RFQRead, RFQVendorRead
from app.services.audit import record_audit

CANCELLABLE = (RFQStatus.DRAFT.value, RFQStatus.PUBLISHED.value)


class RFQError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class RFQNotFound(RFQError):
    status_code = 404


class RFQConflict(RFQError):
    status_code = 409


class RFQInvalid(RFQError):
    status_code = 422


def _request_hash(pr_id: int, actor: str) -> str:
    return hashlib.sha256(json.dumps({"pr_id": pr_id, "actor": actor}, sort_keys=True).encode()).hexdigest()


def _find_by_key(session: Session, pr_id: int, key: str) -> RFQ | None:
    # The key is scoped to the PR: the same key on another PR is a different request.
    return session.scalars(select(RFQ).where(RFQ.pr_id == pr_id, RFQ.idempotency_key == key)).first()


def _replay(existing: RFQ, digest: str) -> tuple[RFQ, bool]:
    if existing.idempotency_hash == digest:
        return existing, False
    raise RFQConflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was already used with a different request")


def _open_rfq(session: Session, pr_id: int) -> RFQ | None:
    return session.scalars(select(RFQ).where(RFQ.pr_id == pr_id, RFQ.status.in_(OPEN_RFQ_STATUSES))).first()


def _open_conflict(open_rfq: RFQ) -> RFQConflict:
    return RFQConflict(
        "OPEN_RFQ_EXISTS", f"PR already has an open RFQ ({open_rfq.rfq_number}); finish or cancel it first",
        rfq_id=open_rfq.id, rfq_status=open_rfq.status,
    )


def _rfq_snapshot(rfq: RFQ) -> dict:
    return {
        "id": rfq.id, "rfq_number": rfq.rfq_number, "pr_id": rfq.pr_id, "round_number": rfq.round_number,
        "status": rfq.status, "requested_quantity": rfq.requested_quantity,
        "issue_date": rfq.issue_date.isoformat() if rfq.issue_date else None,
        "response_deadline": rfq.response_deadline.isoformat() if rfq.response_deadline else None,
    }


def to_read(session: Session, rfq: RFQ) -> RFQRead:
    rows = session.execute(
        select(Vendor.vendor_id, Vendor.vendor_name, Vendor.oem_approved)
        .join(RFQVendor, RFQVendor.vendor_id == Vendor.vendor_id)
        .where(RFQVendor.rfq_id == rfq.id).order_by(Vendor.vendor_id)
    ).all()
    vendors = [
        RFQVendorRead(vendor_id=r.vendor_id, vendor_name=r.vendor_name, oem_approved=r.oem_approved,
                      risk_flags=[] if r.oem_approved else ["NON_OEM"])
        for r in rows
    ]
    base = RFQRead.model_validate(rfq).model_dump(exclude={"vendors"})
    return RFQRead(**base, vendors=vendors)


def _check_pr_date(pr: PurchaseRequisition, today: date) -> None:
    if pr.required_delivery_date < today:
        raise RFQConflict(
            "PR_DELIVERY_DATE_PASSED",
            "The PR's required delivery date has already passed; an RFQ would have an unusable deadline",
            required_delivery_date=pr.required_delivery_date.isoformat(),
        )


def generate_rfq(
    session: Session, pr_id: int, actor: str, *,
    idempotency_key: str | None = None, correlation_id: str | None = None, today: date | None = None,
) -> tuple[RFQ, bool]:
    """Create a DRAFT RFQ for the PR's REMAINING quantity. Returns (rfq, created)."""
    today = today or date.today()
    digest = _request_hash(pr_id, actor)
    if idempotency_key is not None:   # replay check FIRST
        existing = _find_by_key(session, pr_id, idempotency_key)
        if existing is not None:
            return _replay(existing, digest)

    pr = session.get(PurchaseRequisition, pr_id)
    if pr is None:
        raise RFQNotFound("PR_NOT_FOUND", f"PR {pr_id} not found")
    if pr.status != PRStatus.APPROVED.value:
        raise RFQConflict("PR_NOT_APPROVED", f"PR is {pr.status}; an RFQ needs an APPROVED PR", pr_status=pr.status)
    _check_pr_date(pr, today)
    open_rfq = _open_rfq(session, pr_id)
    if open_rfq is not None:
        if idempotency_key is not None:   # the open RFQ may be our own twin's, committed since the key check
            existing = _find_by_key(session, pr_id, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        raise _open_conflict(open_rfq)
    remaining = pr.quantity - pr.ordered_quantity   # ordered_quantity is the only source of truth
    if remaining <= 0:
        raise RFQConflict("NO_REMAINING_QUANTITY", "Every unit of this PR is already covered by purchase orders",
                          quantity=pr.quantity, ordered_quantity=pr.ordered_quantity)
    round_number = 1 + (session.scalar(select(func.max(RFQ.round_number)).where(RFQ.pr_id == pr_id)) or 0)

    rfq = RFQ(
        pr_id=pr_id, round_number=round_number, status=RFQStatus.DRAFT.value, requested_quantity=remaining,
        idempotency_key=idempotency_key, idempotency_hash=digest if idempotency_key is not None else None,
    )
    session.add(rfq)
    try:
        session.flush()   # trips the partial unique index / (pr, round) / (pr, key) if we lost a race
        record_audit(
            session, actor=actor, action="RFQ_GENERATED", entity_type="RFQ", entity_id=rfq.id,
            before=None, after=_rfq_snapshot(rfq), correlation_id=correlation_id,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        if idempotency_key is not None:
            existing = _find_by_key(session, pr_id, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        open_rfq = _open_rfq(session, pr_id)
        if open_rfq is not None:
            raise _open_conflict(open_rfq)
        raise
    except Exception:
        session.rollback()
        raise
    return rfq, True


def publish_rfq(
    session: Session, rfq_id: int, payload: RFQPublish, *,
    correlation_id: str | None = None, today: date | None = None,
) -> RFQ:
    """Invite vendors and issue the RFQ (DRAFT -> PUBLISHED). Records invitations only."""
    today = today or date.today()
    rfq = session.get(RFQ, rfq_id)
    if rfq is None:
        raise RFQNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    if rfq.status != RFQStatus.DRAFT.value:
        raise RFQConflict("RFQ_NOT_DRAFT", f"RFQ is {rfq.status}; only a DRAFT can be published", rfq_status=rfq.status)
    pr = session.get(PurchaseRequisition, rfq.pr_id)
    _check_pr_date(pr, today)
    earliest, latest = today + timedelta(days=1), pr.required_delivery_date
    if not (earliest <= payload.response_deadline <= latest):
        raise RFQInvalid(
            "INVALID_DEADLINE",
            "response_deadline must be after today and not after the PR's required delivery date",
            earliest=earliest.isoformat(), latest=latest.isoformat(),
        )

    ids = payload.vendor_ids
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RFQInvalid("DUPLICATE_VENDORS", "vendor_ids contains duplicates", duplicates=duplicates)
    item = session.get(Item, pr.item_id)
    found = {v.vendor_id: v for v in session.scalars(select(Vendor).where(Vendor.vendor_id.in_(ids)))}
    unknown = [i for i in ids if i not in found]
    inactive = [i for i in ids if i in found and not found[i].is_active]
    wrong_category = [i for i in ids if i in found and found[i].category != item.category]
    if unknown or inactive or wrong_category:
        raise RFQInvalid(
            "INVALID_VENDORS", "Every invited vendor must exist, be active and be in the item's category",
            unknown=unknown, inactive=inactive, wrong_category=wrong_category, item_category=item.category,
        )

    before = {"status": rfq.status}
    try:
        result = session.execute(
            update(RFQ).where(RFQ.id == rfq.id, RFQ.status == RFQStatus.DRAFT.value)
            .values(status=RFQStatus.PUBLISHED.value, issue_date=today, response_deadline=payload.response_deadline)
        )
        if result.rowcount != 1:
            session.rollback()
            now = session.scalar(select(RFQ.status).where(RFQ.id == rfq_id))
            raise RFQConflict("RFQ_NOT_DRAFT", "RFQ was changed by someone else at the same moment", rfq_status=now)
        session.add_all([RFQVendor(rfq_id=rfq.id, vendor_id=i) for i in ids])
        session.flush()
        session.refresh(rfq)
        record_audit(
            session, actor=payload.actor, action="RFQ_PUBLISHED", entity_type="RFQ", entity_id=rfq.id,
            before=before,
            after={**_rfq_snapshot(rfq),
                   "vendors": [{"vendor_id": i, "oem_approved": found[i].oem_approved} for i in ids]},
            correlation_id=correlation_id,
        )
        session.commit()
    except RFQError:
        raise
    except Exception:
        session.rollback()
        raise
    return rfq


def cancel_rfq(
    session: Session, rfq_id: int, payload: RFQCancel, *, correlation_id: str | None = None,
) -> RFQ:
    """Cancel a DRAFT or PUBLISHED RFQ that has no quotations yet (reason required)."""
    rfq = session.get(RFQ, rfq_id)
    if rfq is None:
        raise RFQNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    if rfq.status == RFQStatus.CANCELLED.value:
        raise RFQConflict("RFQ_ALREADY_CANCELLED", "RFQ is already CANCELLED", rfq_status=rfq.status)
    if rfq.status not in CANCELLABLE:
        raise RFQConflict("RFQ_NOT_CANCELLABLE", f"RFQ is {rfq.status}; only DRAFT or PUBLISHED can be cancelled",
                          rfq_status=rfq.status)
    quotes = session.scalar(select(func.count()).select_from(Quotation).where(Quotation.rfq_id == rfq_id))
    if quotes:
        raise RFQConflict("RFQ_HAS_QUOTATIONS", "Quotations already exist for this RFQ", rfq_status=rfq.status,
                          quotation_count=quotes)

    before = {"status": rfq.status}
    try:
        # ONE statement re-checks both conditions, so a quotation arriving right now cannot slip in.
        result = session.execute(
            update(RFQ)
            .where(RFQ.id == rfq.id, RFQ.status.in_(CANCELLABLE),
                   ~exists().where(Quotation.rfq_id == RFQ.id))
            .values(status=RFQStatus.CANCELLED.value)
        )
        if result.rowcount != 1:
            session.rollback()
            now = session.scalar(select(RFQ.status).where(RFQ.id == rfq_id))
            raise RFQConflict("RFQ_CHANGED", "RFQ changed at the same moment; read its state and decide again",
                              rfq_status=now)
        session.refresh(rfq)
        record_audit(
            session, actor=payload.actor, action="RFQ_CANCELLED", entity_type="RFQ", entity_id=rfq.id,
            before=before, after={**_rfq_snapshot(rfq), "reason": payload.reason}, correlation_id=correlation_id,
        )
        session.commit()
    except RFQError:
        raise
    except Exception:
        session.rollback()
        raise
    return rfq