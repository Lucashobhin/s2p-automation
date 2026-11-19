"""QCS lifecycle (Phase 3f): create, edit, submit, decide, withdraw.

Every state change is ONE transaction: QCS change + audit row (D14). Errors carry a machine-readable
code and the current state (D61). Identities are NOT verified: every name check is a speed bump.
Block A (this part): errors, create (idempotent), facts about the recommendation, reads.
"""
import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import rules
from app.core.enums import ACTIVE_PO_STATUSES, ApprovalDecision, QCSStatus, QuotationStatus, RFQStatus
from app.models.approval import Approval
from app.models.purchase_order import PurchaseOrder
from app.models.purchase_requisition import PurchaseRequisition
from app.models.qcs import QCS
from app.models.quotation import Quotation
from app.models.rfq import RFQ
from app.models.vendor import Vendor
from app.schemas.qcs import (
    QCSApprovalRead, QCSCreate, QCSDecision, QCSDecisionResult, QCSRead, QCSRecommendation,
    QCSSubmit, QCSUpdate, QCSWithdraw,
)
from app.services.audit import record_audit
from app.services.comparison import PERCENT, UNIT


class QCSError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class QCSNotFound(QCSError):
    status_code = 404


class QCSForbidden(QCSError):
    status_code = 403


class QCSConflict(QCSError):
    status_code = 409


class QCSInvalid(QCSError):
    status_code = 422


# ---------- small helpers ----------

def _norm(name: str) -> str:
    return name.strip().casefold()


def _create_hash(payload: QCSCreate) -> str:
    """Hash of the NORMALISED create request (the schema already trimmed everything)."""
    canonical = {
        "created_by": payload.created_by,
        "recommended_quotation_id": payload.recommended_quotation_id,
        "justification": payload.justification,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _find_by_key(session: Session, rfq_id: int, key: str) -> QCS | None:
    # The key is scoped to the RFQ: the same key on another RFQ is a different request.
    return session.scalars(select(QCS).where(QCS.rfq_id == rfq_id, QCS.idempotency_key == key)).first()


def _replay(existing: QCS, digest: str) -> tuple[QCS, bool]:
    if existing.request_hash == digest:
        return existing, False
    raise QCSConflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was already used with a different request")


def _own_twin(session: Session, rfq_id: int, key: str | None, digest: str) -> tuple[QCS, bool] | None:
    """Every refuse path asks first: is the thing in my way my own earlier request?"""
    if key is None:
        return None
    existing = _find_by_key(session, rfq_id, key)
    return None if existing is None else _replay(existing, digest)


def _snapshot(qcs: QCS) -> dict:
    return {
        "id": qcs.id, "rfq_id": qcs.rfq_id, "status": qcs.status, "created_by": qcs.created_by,
        "recommended_quotation_id": qcs.recommended_quotation_id, "justification": qcs.justification,
    }


# ---------- facts about the recommendation (D49, D50, D53) ----------

def _unit_incl(q: Quotation) -> Decimal:
    return (q.total_amount / q.quoted_quantity).quantize(UNIT, rounding=ROUND_HALF_UP)


def validate_recommendation(session: Session, rfq: RFQ, quotation_id: int) -> Quotation:
    """The pick must be an ACTIVE quotation of THIS RFQ (the composite FK covers 'same RFQ' too)."""
    q = session.get(Quotation, quotation_id)
    if q is None or q.rfq_id != rfq.id:
        raise QCSInvalid("QUOTATION_NOT_IN_RFQ", f"Quotation {quotation_id} does not belong to RFQ {rfq.id}",
                         quotation_id=quotation_id)
    if q.status != QuotationStatus.ACTIVE.value:
        raise QCSInvalid("QUOTATION_NOT_ACTIVE", "Only a vendor's current (ACTIVE) quotation can be recommended",
                         quotation_id=quotation_id, quotation_status=q.status)
    return q


def build_recommendation(session: Session, rfq: RFQ, q: Quotation) -> QCSRecommendation:
    vendor = session.scalars(select(Vendor).where(Vendor.vendor_id == q.vendor_id)).one()
    active = session.scalars(
        select(Quotation).where(Quotation.rfq_id == rfq.id, Quotation.status == QuotationStatus.ACTIVE.value)
    ).all()
    best = min((_unit_incl(x) for x in active), default=None)
    unit = _unit_incl(q)
    return QCSRecommendation(
        quotation_id=q.id, vendor_id=vendor.vendor_id, vendor_name=vendor.vendor_name, revision=q.revision,
        quoted_quantity=q.quoted_quantity,
        coverage_percent=(Decimal(q.quoted_quantity) * 100 / Decimal(rfq.requested_quantity)).quantize(
            PERCENT, rounding=ROUND_HALF_UP),
        unit_price_incl_tax=unit, total_amount=q.total_amount,
        is_lowest_unit_price_incl_tax=best is None or unit <= best,   # ties count as lowest
        is_partial=q.quoted_quantity < rfq.requested_quantity,
    )


def justification_rule(rec) -> tuple[int, list[str]]:
    """(minimum justification length, reasons it is stricter). Length is a speed bump, not proof (D50)."""
    reasons = []
    if not rec.is_lowest_unit_price_incl_tax:
        reasons.append("NOT_LOWEST_UNIT_PRICE")
    if rec.is_partial:
        reasons.append("PARTIAL_QUOTE")
    length = rules.MIN_JUSTIFICATION_LENGTH_STRICT if reasons else rules.MIN_JUSTIFICATION_LENGTH
    return length, reasons


# ---------- reads ----------

def get_qcs(session: Session, qcs_id: int) -> QCS:
    qcs = session.get(QCS, qcs_id)
    if qcs is None:
        raise QCSNotFound("QCS_NOT_FOUND", f"QCS {qcs_id} not found")
    return qcs


def get_qcs_for_rfq(session: Session, rfq_id: int) -> QCS:
    if session.get(RFQ, rfq_id) is None:
        raise QCSNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    qcs = session.scalars(select(QCS).where(QCS.rfq_id == rfq_id)).first()
    if qcs is None:
        raise QCSNotFound("QCS_NOT_FOUND", f"RFQ {rfq_id} has no QCS yet")
    return qcs


def to_read(session: Session, qcs: QCS) -> QCSRead:
    rfq = session.get(RFQ, qcs.rfq_id)
    rec = min_len = levels = None
    if qcs.recommended_quotation_id is not None:
        q = session.get(Quotation, qcs.recommended_quotation_id)
        rec = build_recommendation(session, rfq, q)
        min_len = justification_rule(rec)[0]
        levels = rules.required_qcs_approval_levels(q)
    approved = session.scalar(
        select(func.count()).select_from(Approval)
        .where(Approval.qcs_id == qcs.id, Approval.decision == ApprovalDecision.APPROVED.value)
    )
    return QCSRead(
        id=qcs.id, rfq_id=qcs.rfq_id, rfq_number=rfq.rfq_number, rfq_status=rfq.status, status=qcs.status,
        created_by=qcs.created_by, created_at=qcs.created_at, updated_at=qcs.updated_at,
        recommended_quotation_id=qcs.recommended_quotation_id, justification=qcs.justification,
        recommendation=rec, justification_min_length=min_len, levels_required=levels, levels_approved=approved,
    )


def list_approvals(session: Session, qcs_id: int) -> list[Approval]:
    get_qcs(session, qcs_id)   # 404 for an unknown QCS
    return list(session.scalars(
        select(Approval).where(Approval.qcs_id == qcs_id).order_by(Approval.level, Approval.id)))


# ---------- create (D48) ----------

def create_qcs(
    session: Session, rfq_id: int, payload: QCSCreate, *,
    idempotency_key: str | None = None, correlation_id: str | None = None,
) -> tuple[QCS, bool]:
    """Create a DRAFT QCS for a CLOSED RFQ. Returns (qcs, created); created=False is an identical replay.

    Order matters: the replay check comes FIRST, so a retry still gets its answer after the RFQ has moved on.
    """
    _clean_name(payload.created_by, "created_by")   # the schema refuses blank too; the service does not rely on it (D55, D63)
    digest = _create_hash(payload)
    twin = _own_twin(session, rfq_id, idempotency_key, digest)
    if twin is not None:
        return twin

    rfq = session.get(RFQ, rfq_id)
    if rfq is None:
        raise QCSNotFound("RFQ_NOT_FOUND", f"RFQ {rfq_id} not found")
    if rfq.status != RFQStatus.CLOSED.value:
        twin = _own_twin(session, rfq_id, idempotency_key, digest)
        if twin is not None:
            return twin
        raise QCSConflict("RFQ_NOT_CLOSED", f"RFQ is {rfq.status}; a QCS needs a CLOSED RFQ", rfq_status=rfq.status)
    existing = session.scalars(select(QCS).where(QCS.rfq_id == rfq_id)).first()
    if existing is not None:
        twin = _own_twin(session, rfq_id, idempotency_key, digest)
        if twin is not None:
            return twin
        raise QCSConflict("QCS_EXISTS", "This RFQ already has a QCS", qcs_id=existing.id, qcs_status=existing.status)
    if payload.recommended_quotation_id is not None:
        validate_recommendation(session, rfq, payload.recommended_quotation_id)

    qcs = QCS(
        rfq_id=rfq_id, created_by=payload.created_by, recommended_quotation_id=payload.recommended_quotation_id,
        justification=payload.justification, status=QCSStatus.DRAFT.value,
        idempotency_key=idempotency_key, request_hash=digest if idempotency_key is not None else None,
    )
    session.add(qcs)
    try:
        session.flush()   # trips UNIQUE (rfq_id) / (rfq_id, key) / the composite FK if we lost a race
        record_audit(
            session, actor=payload.created_by, action="QCS_CREATED", entity_type="QCS", entity_id=qcs.id,
            before=None, after=_snapshot(qcs), correlation_id=correlation_id,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        twin = _own_twin(session, rfq_id, idempotency_key, digest)
        if twin is not None:
            return twin
        existing = session.scalars(select(QCS).where(QCS.rfq_id == rfq_id)).first()
        if existing is not None:
            raise QCSConflict("QCS_EXISTS", "This RFQ already has a QCS", qcs_id=existing.id,
                              qcs_status=existing.status)
        raise
    except Exception:
        session.rollback()
        raise
    return qcs, True


# ---------- block B: edit (D51, D64) and submit (D52, D65) ----------

def _clean_name(value, field: str) -> str:
    """Trim; refuse blank. The schema already does this, but the service does not rely on it (D63)."""
    if not isinstance(value, str) or not value.strip():
        raise QCSInvalid("NAME_REQUIRED", f"{field} is required", field=field)
    return value.strip()


def _require_creator(qcs: QCS, actor: str) -> None:
    # A QCS with no creator (made by direct SQL) can be changed by nobody through the API.
    if qcs.created_by is None or _norm(qcs.created_by) != _norm(actor):
        raise QCSForbidden("NOT_THE_CREATOR",
                           "Only the QCS creator may do this (a name check; identities are unverified)")


def _require_draft(qcs: QCS) -> None:
    if qcs.status != QCSStatus.DRAFT.value:
        raise QCSConflict("QCS_NOT_DRAFT", f"QCS is {qcs.status}; only a DRAFT can be edited or submitted",
                          qcs_status=qcs.status)


def _require_closed(rfq: RFQ) -> None:
    if rfq.status != RFQStatus.CLOSED.value:
        raise QCSConflict("RFQ_NOT_CLOSED", f"RFQ is {rfq.status}; a QCS can only change while its RFQ is CLOSED",
                          rfq_status=rfq.status)


def _same(column, value):
    """column = value, treating None as SQL NULL."""
    return column.is_(None) if value is None else column == value


def _rfq_is_closed():
    return exists().where(RFQ.id == QCS.rfq_id, RFQ.status == RFQStatus.CLOSED.value)


def _lost_race(session: Session, qcs_id: int) -> QCSError:
    """A conditional UPDATE matched nothing. Read the state AFTER it and name what really happened (D66)."""
    session.rollback()
    session.expire_all()
    qcs = session.get(QCS, qcs_id)
    if qcs.status != QCSStatus.DRAFT.value:
        return QCSConflict("QCS_NOT_DRAFT", f"QCS is {qcs.status}; it changed at the same moment",
                           qcs_status=qcs.status)
    rfq = session.get(RFQ, qcs.rfq_id)
    if rfq.status != RFQStatus.CLOSED.value:
        return QCSConflict("RFQ_NOT_CLOSED", f"RFQ is {rfq.status}; it changed at the same moment",
                           rfq_status=rfq.status)
    if qcs.recommended_quotation_id is not None:
        q = session.get(Quotation, qcs.recommended_quotation_id)
        if q is None or q.status != QuotationStatus.ACTIVE.value:
            return QCSInvalid("QUOTATION_NOT_ACTIVE", "The recommended quotation is no longer ACTIVE",
                              quotation_id=qcs.recommended_quotation_id,
                              quotation_status=None if q is None else q.status)
    return QCSConflict("QCS_CHANGED", "The QCS was edited by someone else at the same moment; read it and decide again",
                       qcs_status=qcs.status)


def update_qcs(session: Session, qcs_id: int, payload: QCSUpdate, *, correlation_id: str | None = None) -> QCS:
    """PATCH a DRAFT QCS. Only the fields SENT change; null clears one. Nothing to change = no write, no audit."""
    actor = _clean_name(payload.actor, "actor")
    qcs = get_qcs(session, qcs_id)              # D63 order: 404, 403, 409 (state), 409 (RFQ), 422
    _require_creator(qcs, actor)
    _require_draft(qcs)
    rfq = session.get(RFQ, qcs.rfq_id)
    _require_closed(rfq)

    sent = payload.model_fields_set - {"actor"}
    if not sent:
        raise QCSInvalid("NOTHING_TO_UPDATE", "Send recommended_quotation_id and/or justification (null clears a field)")

    old = {"recommended_quotation_id": qcs.recommended_quotation_id, "justification": qcs.justification}
    new = {name: getattr(payload, name) for name in sent}
    if new.get("recommended_quotation_id") is not None:
        validate_recommendation(session, rfq, new["recommended_quotation_id"])

    changes = {name: value for name, value in new.items() if value != old[name]}
    if not changes:
        return qcs

    conditions = [
        QCS.id == qcs.id,
        QCS.status == QCSStatus.DRAFT.value,
        _rfq_is_closed(),
    ]
    conditions += [_same(getattr(QCS, name), old[name]) for name in changes]  # nobody changed these fields meanwhile

    try:
        result = session.execute(update(QCS).where(*conditions).values(**changes))
        if result.rowcount != 1:
            raise _lost_race(session, qcs_id)
        session.refresh(qcs)
        record_audit(
            session, actor=actor, action="QCS_UPDATED", entity_type="QCS", entity_id=qcs.id,
            before={name: old[name] for name in changes}, after=dict(changes),
            correlation_id=correlation_id,
        )
        session.commit()
    except QCSError:
        raise
    except Exception:
        session.rollback()
        raise
    return qcs


def submit_qcs(session: Session, qcs_id: int, payload: QCSSubmit, *, correlation_id: str | None = None) -> QCS:
    """DRAFT -> PENDING_APPROVAL. Everything is validated again, then ONE UPDATE re-checks it all (D65)."""
    actor = _clean_name(payload.actor, "actor")
    qcs = get_qcs(session, qcs_id)
    _require_creator(qcs, actor)
    _require_draft(qcs)
    rfq = session.get(RFQ, qcs.rfq_id)
    _require_closed(rfq)

    if qcs.recommended_quotation_id is None:
        raise QCSInvalid("RECOMMENDATION_REQUIRED", "Choose a recommended quotation before submitting")
    q = validate_recommendation(session, rfq, qcs.recommended_quotation_id)

    justification = qcs.justification
    if not justification:
        raise QCSInvalid("JUSTIFICATION_REQUIRED", "A justification is required before submitting")

    rec = build_recommendation(session, rfq, q)
    min_len, reasons = justification_rule(rec)
    if len(justification) < min_len:
        raise QCSInvalid(
            "JUSTIFICATION_TOO_SHORT", f"The justification needs at least {min_len} characters",
            min_length=min_len, current_length=len(justification), reasons=reasons,
        )

    levels = rules.required_qcs_approval_levels(q)
    conditions = [
        QCS.id == qcs.id,
        QCS.status == QCSStatus.DRAFT.value,
        QCS.recommended_quotation_id == q.id,
        QCS.justification == justification,
        _rfq_is_closed(),
        exists().where(Quotation.id == QCS.recommended_quotation_id, Quotation.status == QuotationStatus.ACTIVE.value),
    ]
    try:
        result = session.execute(update(QCS).where(*conditions).values(status=QCSStatus.PENDING_APPROVAL.value))
        if result.rowcount != 1:
            raise _lost_race(session, qcs_id)
        session.refresh(qcs)
        record_audit(
            session, actor=actor, action="QCS_SUBMITTED", entity_type="QCS", entity_id=qcs.id,
            before={"status": QCSStatus.DRAFT.value},
            after={
                "status": qcs.status, "recommended_quotation_id": q.id,
                "is_lowest_unit_price_incl_tax": rec.is_lowest_unit_price_incl_tax,
                "is_partial": rec.is_partial, "justification_min_length": min_len,
                "reasons": reasons, "levels_required": levels,
            },
            correlation_id=correlation_id,
        )
        session.commit()
    except QCSError:
        raise
    except Exception:
        session.rollback()
        raise
    return qcs




# ---------- block C: decision (D67-D74) ----------

log = logging.getLogger(__name__)
FINAL_QCS = (QCSStatus.APPROVED.value, QCSStatus.REJECTED.value, QCSStatus.WITHDRAWN.value)


class _LostRace(Exception):
    pass


def _decision_hash(payload: QCSDecision) -> str:
    """Hash of the NORMALISED request: approver (trimmed, case-folded), decision and comments."""
    canonical = {
        "approver": _norm(payload.approver),
        "decision": payload.decision.value,
        "comments": payload.comments,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _find_decision_by_key(session: Session, qcs_id: int, key: str) -> Approval | None:
    # The key is scoped to the QCS: the same key on another QCS is a different request.
    return session.scalars(
        select(Approval).where(Approval.qcs_id == qcs_id, Approval.idempotency_key == key)
    ).first()


def _decision_replay(existing: Approval, payload: QCSDecision, digest: str) -> tuple[Approval, bool]:
    """Only the SAME approver (name-normalised) with the SAME content gets the stored approval back (D73).
    Everyone else gets one neutral refusal that never says whose key it was."""
    if _norm(existing.approver) != _norm(payload.approver) or existing.request_hash != digest:
        raise QCSConflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key was already used with a different request")
    return existing, False


def _own_decision_twin(session: Session, qcs_id: int, key: str | None, payload: QCSDecision, digest: str):
    if key is None:
        return None
    existing = _find_decision_by_key(session, qcs_id, key)
    return None if existing is None else _decision_replay(existing, payload, digest)


def _check_separation(session: Session, qcs: QCS, rfq: RFQ, approver: str) -> None:
    """D68: names only (identities are unverified). A legacy QCS with no creator skips the creator rule."""
    me = _norm(approver)
    if qcs.created_by is not None and _norm(qcs.created_by) == me:
        raise QCSForbidden("SEPARATION_OF_DUTIES", "The approver may not be the QCS creator (a name check)",
                           reason="APPROVER_IS_CREATOR")
    pr = session.get(PurchaseRequisition, rfq.pr_id)
    if _norm(pr.requester) == me:
        raise QCSForbidden("SEPARATION_OF_DUTIES", "The approver may not be the requester of the PR (a name check)",
                           reason="APPROVER_IS_REQUESTER")
    names = session.scalars(select(Approval.approver).where(Approval.qcs_id == qcs.id)).all()
    if any(_norm(name) == me for name in names):
        raise QCSForbidden("SEPARATION_OF_DUTIES", "This approver already decided a level of this QCS (a name check)",
                           reason="APPROVER_ALREADY_DECIDED_A_LEVEL")


def _log_refused(session: Session, qcs_id: int, approver: str, payload: QCSDecision, reason: str,
                 qcs_status: str, correlation_id: str | None) -> None:
    """D72: record a refused attempt in its OWN transaction. If that fails, the caller's 409 still stands."""
    try:
        session.rollback()
        record_audit(
            session, actor=approver, action="QCS_DUPLICATE_DECISION_ATTEMPT", entity_type="QCS", entity_id=qcs_id,
            before=None, after={"reason": reason, "attempted_decision": payload.decision.value,
                                "qcs_status": qcs_status},
            correlation_id=correlation_id,
        )
        session.commit()
    except Exception:
        session.rollback()
        log.warning("could not record the refused decision attempt on QCS %s", qcs_id, exc_info=True)


def _decision_lost(session: Session, qcs_id: int, approver: str, payload: QCSDecision, correlation_id) -> QCSError:
    """The UPDATE or an insert lost a race. Read the REAL state after it and name it (D71)."""
    session.expire_all()
    qcs = session.get(QCS, qcs_id)
    status_now = qcs.status
    rfq = session.get(RFQ, qcs.rfq_id)
    if status_now != QCSStatus.PENDING_APPROVAL.value:
        err = QCSConflict("QCS_NOT_PENDING", f"QCS is {status_now}; another decision was recorded first",
                          qcs_status=status_now)
    elif rfq.status != RFQStatus.CLOSED.value:
        err = QCSConflict("RFQ_NOT_CLOSED", f"RFQ is {rfq.status}; it changed at the same moment",
                          rfq_status=rfq.status)
    else:
        err = QCSConflict("QCS_CHANGED", "Another decision at this level was recorded first; read the QCS and decide again",
                          qcs_status=status_now)
    _log_refused(session, qcs_id, approver, payload, "CONCURRENT_DECISION", status_now, correlation_id)
    return err


def decide_qcs(
    session: Session, qcs_id: int, payload: QCSDecision, *,
    idempotency_key: str | None = None, correlation_id: str | None = None,
) -> tuple[Approval, bool]:
    """Returns (approval, created). created=False is an idempotent replay.

    Order (D67): replay by key, 404, 403, 409 state, 409 RFQ. The level is derived by the server (D69).
    """
    approver = _clean_name(payload.approver, "approver")
    digest = _decision_hash(payload)
    twin = _own_decision_twin(session, qcs_id, idempotency_key, payload, digest)
    if twin is not None:   # replay check FIRST, before any state rule
        return twin

    qcs = get_qcs(session, qcs_id)
    rfq = session.get(RFQ, qcs.rfq_id)
    try:
        _check_separation(session, qcs, rfq, approver)
    except QCSForbidden:
        twin = _own_decision_twin(session, qcs_id, idempotency_key, payload, digest)   # my own twin may have committed
        if twin is not None:
            return twin
        raise
    if qcs.status != QCSStatus.PENDING_APPROVAL.value:
        twin = _own_decision_twin(session, qcs_id, idempotency_key, payload, digest)
        if twin is not None:
            return twin
        status_now = qcs.status
        if status_now in FINAL_QCS:
            _log_refused(session, qcs_id, approver, payload, "QCS_ALREADY_DECIDED", status_now, correlation_id)
        raise QCSConflict("QCS_NOT_PENDING", f"QCS is {status_now}; decisions are only accepted while it is PENDING_APPROVAL",
                          qcs_status=status_now)
    if rfq.status != RFQStatus.CLOSED.value:
        twin = _own_decision_twin(session, qcs_id, idempotency_key, payload, digest)
        if twin is not None:
            return twin
        raise QCSConflict("RFQ_NOT_CLOSED", f"RFQ is {rfq.status}; a QCS can only be decided while its RFQ is CLOSED",
                          rfq_status=rfq.status)
    if qcs.recommended_quotation_id is None:
        raise QCSInvalid("RECOMMENDATION_REQUIRED", "This QCS has no recommended quotation")

    picked = session.get(Quotation, qcs.recommended_quotation_id)
    required = rules.required_qcs_approval_levels(picked)
    level = session.scalar(
        select(func.count()).select_from(Approval)
        .where(Approval.qcs_id == qcs.id, Approval.decision == ApprovalDecision.APPROVED.value)
    ) + 1
    decision = payload.decision
    if decision == ApprovalDecision.APPROVED:
        final = level >= required
        new_status = QCSStatus.APPROVED.value if final else QCSStatus.PENDING_APPROVAL.value
        action = "QCS_APPROVED" if final else "QCS_APPROVAL_LEVEL_APPROVED"
    else:
        new_status, action = QCSStatus.REJECTED.value, "QCS_REJECTED"
    moves = new_status != QCSStatus.PENDING_APPROVAL.value
    values = {"status": new_status} if moves else {"updated_at": QCS.updated_at}   # D70: no-op write, no new updated_at
    if moves:
        before = {"status": QCSStatus.PENDING_APPROVAL.value}
        after = {"status": new_status, "level": level, "levels_required": required,
                 "decision": decision.value, "comments": payload.comments}
    else:
        before = None   # the QCS did not change state: no status transition is implied
        after = {"qcs_status": QCSStatus.PENDING_APPROVAL.value, "level": level, "levels_required": required,
                 "decision": decision.value, "comments": payload.comments}

    approval = Approval(
        qcs_id=qcs.id, level=level, approval_cycle=1, approver=approver, decision=decision.value,
        comments=payload.comments, idempotency_key=idempotency_key,
        request_hash=digest if idempotency_key is not None else None,
    )
    try:
        # The conditional UPDATE comes FIRST: it fails fast, and it re-checks the state and the RFQ.
        result = session.execute(
            update(QCS)
            .where(QCS.id == qcs.id, QCS.status == QCSStatus.PENDING_APPROVAL.value, _rfq_is_closed())
            .values(**values)
        )
        if result.rowcount != 1:
            raise _LostRace()
        session.add(approval)
        session.flush()   # trips UNIQUE (qcs, level, cycle) / (qcs, key) if we lost a race
        record_audit(
            session, actor=approver, action=action, entity_type="QCS", entity_id=qcs.id,
            before=before, after=after, correlation_id=correlation_id,
        )
        session.commit()
    except (IntegrityError, _LostRace):
        session.rollback()
        twin = _own_decision_twin(session, qcs_id, idempotency_key, payload, digest)
        if twin is not None:
            return twin
        raise _decision_lost(session, qcs_id, approver, payload, correlation_id)
    except Exception:
        session.rollback()
        raise
    return approval, True


def to_decision_result(session: Session, approval: Approval) -> QCSDecisionResult:
    """The approval as stored, plus the QCS status NOW (for a replay: its current status)."""
    qcs = session.get(QCS, approval.qcs_id)
    picked = session.get(Quotation, qcs.recommended_quotation_id)
    base = QCSApprovalRead.model_validate(approval).model_dump()
    return QCSDecisionResult(**base, qcs_status=qcs.status, levels_required=rules.required_qcs_approval_levels(picked))