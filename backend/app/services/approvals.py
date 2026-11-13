"""PR approval workflow (Phase 3b): decide (approve / reject / return) and resubmit.

Every operation is ONE transaction: approval row + PR status change + audit row (D14).
The client never chooses the level: it is derived from what was already decided in the
current cycle. A RETURNED decision closes a cycle; cycle = 1 + number of RETURNED decisions.
"""
import hashlib
import json
from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import rules
from app.core.enums import ApprovalDecision, PRStatus
from app.models.approval import Approval
from app.models.purchase_requisition import PurchaseRequisition
from app.schemas.approval import PRDecision
from app.schemas.requisition import PRCreate
from app.services.audit import record_audit
from app.services.requisitions import IdempotencyConflict, check_business_rules, pr_snapshot

FINAL_STATUSES = (PRStatus.APPROVED.value, PRStatus.REJECTED.value)


class ApprovalError(Exception):
    pass


class PRNotFound(ApprovalError):
    pass


class SeparationOfDuties(ApprovalError):
    pass


class NotTheRequester(ApprovalError):
    pass


class WrongState(ApprovalError):
    pass


class DecisionConflict(ApprovalError):
    pass


class _LostRace(Exception):
    pass


def _norm(name: str) -> str:
    return name.strip().casefold()


def request_hash(payload: PRDecision) -> str:
    """Hash of the NORMALISED request: approver, decision AND comments."""
    canonical = {
        "approver": payload.approver,
        "decision": payload.decision.value,
        "comments": payload.comments,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def current_cycle(session: Session, pr_id: int) -> int:
    returned = session.scalar(
        select(func.count()).select_from(Approval)
        .where(Approval.pr_id == pr_id, Approval.decision == ApprovalDecision.RETURNED.value)
    )
    return 1 + returned


def approved_levels_in_cycle(session: Session, pr_id: int, cycle: int) -> int:
    return session.scalar(
        select(func.count()).select_from(Approval)
        .where(Approval.pr_id == pr_id, Approval.approval_cycle == cycle,
               Approval.decision == ApprovalDecision.APPROVED.value)
    )


def _find_by_key(session: Session, pr_id: int, key: str) -> Approval | None:
    # The key is scoped to the PR: the same key on another PR is a different request.
    return session.scalars(
        select(Approval).where(Approval.pr_id == pr_id, Approval.idempotency_key == key)
    ).first()


def _replay(existing: Approval, digest: str) -> tuple[Approval, bool]:
    if existing.request_hash == digest:
        return existing, False
    raise IdempotencyConflict("Idempotency-Key was already used with a different request body")


def _log_refused(session: Session, pr_id: int, payload: PRDecision, reason: str,
                 pr_status: str | None, correlation_id: str | None) -> None:
    """Record a refused attempt in its OWN transaction (the failed one was rolled back)."""
    session.rollback()
    record_audit(
        session,
        actor=payload.approver,
        action="APPROVAL_DUPLICATE_ATTEMPT",
        entity_type="PR",
        entity_id=pr_id,
        before=None,
        after={"reason": reason, "attempted_decision": payload.decision.value, "pr_status": pr_status},
        correlation_id=correlation_id,
    )
    session.commit()


def decide_requisition(
    session: Session,
    pr_id: int,
    payload: PRDecision,
    *,
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
) -> tuple[Approval, bool]:
    """Returns (approval, created). created=False is an idempotent replay."""
    digest = request_hash(payload)

    if idempotency_key is not None:   # replay check FIRST, before any state rule
        existing = _find_by_key(session, pr_id, idempotency_key)
        if existing is not None:
            return _replay(existing, digest)

    pr = session.get(PurchaseRequisition, pr_id)
    if pr is None:
        raise PRNotFound(f"PR {pr_id} not found")
    if _norm(payload.approver) == _norm(pr.requester):
        raise SeparationOfDuties(
            "The approver may not be the requester. This compares names only (identities are "
            "unverified); it does not prove that anyone is a manager."
        )
    if pr.status != PRStatus.SUBMITTED.value:
        if idempotency_key is not None:   # our own twin may have committed since the first key check
            existing = _find_by_key(session, pr_id, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        if pr.status in FINAL_STATUSES:
            _log_refused(session, pr_id, payload, "PR_ALREADY_DECIDED", pr.status, correlation_id)
        raise WrongState(f"PR is {pr.status}; decisions are only accepted while it is SUBMITTED")

    cycle = current_cycle(session, pr.id)
    level = approved_levels_in_cycle(session, pr.id, cycle) + 1
    required = rules.required_approval_levels(pr)
    decision = payload.decision

    if decision == ApprovalDecision.APPROVED:
        final = level >= required
        new_status = PRStatus.APPROVED.value if final else PRStatus.SUBMITTED.value
        action = "PR_APPROVED" if final else "PR_APPROVAL_LEVEL_APPROVED"
    elif decision == ApprovalDecision.REJECTED:
        new_status, action = PRStatus.REJECTED.value, "PR_REJECTED"
    else:
        new_status, action = PRStatus.RETURNED.value, "PR_RETURNED"
    before_status = pr.status

    approval = Approval(
        pr_id=pr.id, level=level, approval_cycle=cycle, approver=payload.approver,
        decision=decision.value, comments=payload.comments, idempotency_key=idempotency_key,
        request_hash=digest if idempotency_key is not None else None,
    )
    session.add(approval)
    try:
        session.flush()   # trips UNIQUE (pr, level, cycle) / (pr, key) if we lost a race
        if new_status != before_status:
            result = session.execute(
                update(PurchaseRequisition)
                .where(PurchaseRequisition.id == pr.id, PurchaseRequisition.status == PRStatus.SUBMITTED.value)
                .values(status=new_status)
            )
            if result.rowcount != 1:
                raise _LostRace()
        record_audit(
            session,
            actor=payload.approver,
            action=action,
            entity_type="PR",
            entity_id=pr.id,
            before={"status": before_status},
            after={"status": new_status, "level": level, "cycle": cycle, "levels_required": required,
                   "decision": decision.value, "comments": payload.comments},
            correlation_id=correlation_id,
        )
        session.commit()
    except (IntegrityError, _LostRace):
        session.rollback()
        if idempotency_key is not None:       # a retry of ourselves that raced us?
            existing = _find_by_key(session, pr_id, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        _log_refused(session, pr_id, payload, "CONCURRENT_DECISION", None, correlation_id)
        raise DecisionConflict("Another decision for this PR was recorded first")
    except Exception:
        session.rollback()
        raise
    return approval, True


def resubmit_requisition(
    session: Session,
    pr_id: int,
    payload: PRCreate,
    *,
    correlation_id: str | None = None,
    today: date | None = None,
) -> PurchaseRequisition:
    """The requester sends the corrected PR back for a new approval cycle (status RETURNED -> SUBMITTED).

    The body is a complete PR, validated by exactly the same rules as creation.
    """
    today = today or date.today()
    pr = session.get(PurchaseRequisition, pr_id)
    if pr is None:
        raise PRNotFound(f"PR {pr_id} not found")
    if _norm(payload.requester) != _norm(pr.requester):
        raise NotTheRequester("Only the original requester may resubmit (a name check; identities are unverified)")
    if pr.status != PRStatus.RETURNED.value:
        raise WrongState(f"PR is {pr.status}; only a RETURNED PR can be resubmitted")
    check_business_rules(session, payload, today)

    before = pr_snapshot(pr)
    try:
        result = session.execute(
            update(PurchaseRequisition)
            .where(PurchaseRequisition.id == pr.id, PurchaseRequisition.status == PRStatus.RETURNED.value)
            .values(
                department=payload.department, item_id=payload.item_id, quantity=payload.quantity,
                estimated_budget=payload.estimated_budget, currency=payload.currency,
                required_delivery_date=payload.required_delivery_date, status=PRStatus.SUBMITTED.value,
            )
        )
        if result.rowcount != 1:
            raise _LostRace()
        session.refresh(pr)
        record_audit(
            session, actor=pr.requester, action="PR_RESUBMITTED", entity_type="PR", entity_id=pr.id,
            before=before, after=pr_snapshot(pr), correlation_id=correlation_id,
        )
        session.commit()
    except _LostRace:
        session.rollback()
        raise WrongState("PR was changed by someone else at the same moment")
    except Exception:
        session.rollback()
        raise
    return pr