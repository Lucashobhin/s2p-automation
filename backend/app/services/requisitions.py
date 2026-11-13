"""Create a Purchase Requisition (Phase 3a).

One unit of work: PR row + PR_CREATED audit row are committed together or not at all (D14).
Retries: same Idempotency-Key + same payload returns the original PR and writes nothing;
same key + different payload is a conflict. The database UNIQUE on the key settles races.
"""
import hashlib
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.item import Item
from app.models.purchase_requisition import PurchaseRequisition
from app.schemas.requisition import PRCreate
from app.services.audit import record_audit


class RequisitionError(Exception):
    pass


class UnknownItem(RequisitionError):
    pass


class PastDeliveryDate(RequisitionError):
    pass


class IdempotencyConflict(RequisitionError):
    pass


def payload_hash(payload: PRCreate) -> str:
    """Hash of the NORMALISED payload, so harmless formatting differences hash the same."""
    canonical = {
        "requester": payload.requester,
        "department": payload.department,
        "item_id": payload.item_id,
        "quantity": payload.quantity,
        "estimated_budget": format(payload.estimated_budget.normalize(), "f"),
        "currency": payload.currency,
        "required_delivery_date": payload.required_delivery_date.isoformat(),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _find_by_key(session: Session, key: str) -> PurchaseRequisition | None:
    return session.scalars(
        select(PurchaseRequisition).where(PurchaseRequisition.idempotency_key == key)
    ).first()


def _replay(existing: PurchaseRequisition, digest: str) -> tuple[PurchaseRequisition, bool]:
    if existing.idempotency_hash == digest:
        return existing, False
    raise IdempotencyConflict("Idempotency-Key was already used with a different request body")


def pr_snapshot(pr: PurchaseRequisition) -> dict:
    return {
        "id": pr.id,
        "pr_number": pr.pr_number,
        "requester": pr.requester,
        "department": pr.department,
        "item_id": pr.item_id,
        "quantity": pr.quantity,
        "ordered_quantity": pr.ordered_quantity,
        "estimated_budget": str(pr.estimated_budget),
        "currency": pr.currency,
        "required_delivery_date": pr.required_delivery_date.isoformat(),
        "status": pr.status,
    }


def check_business_rules(session: Session, payload: PRCreate, today: date) -> None:
    """The two checks that need today's date or the database. Shared by create and resubmit."""
    if payload.required_delivery_date < today:
        raise PastDeliveryDate("required_delivery_date must be today or later")
    if session.get(Item, payload.item_id) is None:
        raise UnknownItem(f"item {payload.item_id!r} does not exist")


def create_requisition(
    session: Session,
    payload: PRCreate,
    *,
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    today: date | None = None,
) -> tuple[PurchaseRequisition, bool]:
    """Returns (pr, created). created=False means an idempotent replay of an earlier request."""
    today = today or date.today()
    digest = payload_hash(payload)

    # Replay check FIRST: a retry must return the original PR even if its date has passed since.
    if idempotency_key is not None:
        existing = _find_by_key(session, idempotency_key)
        if existing is not None:
            return _replay(existing, digest)

    check_business_rules(session, payload, today)

    pr = PurchaseRequisition(
        requester=payload.requester,
        department=payload.department,
        item_id=payload.item_id,
        quantity=payload.quantity,
        estimated_budget=payload.estimated_budget,
        currency=payload.currency,
        required_delivery_date=payload.required_delivery_date,
        idempotency_key=idempotency_key,
        idempotency_hash=digest if idempotency_key is not None else None,
    )
    session.add(pr)
    try:
        session.flush()  # assigns the id and trips the UNIQUE on the key if a retry raced us
        record_audit(
            session,
            actor=payload.requester,
            action="PR_CREATED",
            entity_type="PR",
            entity_id=pr.id,
            before=None,
            after=pr_snapshot(pr),
            correlation_id=correlation_id,
        )
        session.commit()
    except IntegrityError:
        session.rollback()  # removes the PR AND its audit row
        if idempotency_key is not None:
            existing = _find_by_key(session, idempotency_key)
            if existing is not None:
                return _replay(existing, digest)
        raise
    except Exception:
        session.rollback()
        raise
    return pr, True