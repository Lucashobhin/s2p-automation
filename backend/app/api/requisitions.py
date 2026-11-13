import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import rules
from app.core.rules import HEADER_TOKEN_PATTERN
from app.db.database import get_db
from app.models.approval import Approval
from app.models.purchase_requisition import PurchaseRequisition
from app.schemas.approval import ApprovalRead, DecisionResult, PRDecision
from app.schemas.requisition import PRCreate, PRRead
from app.services.approvals import (
    DecisionConflict, NotTheRequester, PRNotFound, SeparationOfDuties, WrongState,
    decide_requisition, resubmit_requisition,
)
from app.services.requisitions import (
    IdempotencyConflict, PastDeliveryDate, UnknownItem, create_requisition,
)

router = APIRouter(prefix="/requisitions", tags=["requisitions"])
_TOKEN = re.compile(HEADER_TOKEN_PATTERN)

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
CorrelationHeader = Annotated[str | None, Header(alias="X-Correlation-ID")]


def _clean_header(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not _TOKEN.match(value):
        raise HTTPException(422, f"{name} must be 1-64 characters from A-Z a-z 0-9 . _ : -")
    return value


@router.post(
    "",
    response_model=PRRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": PRRead, "description": "Idempotent replay: the original PR"}},
)
def create_pr(
    payload: PRCreate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Create a Purchase Requisition (status SUBMITTED).

    The requester is user-supplied and NOT verified: there is no login yet.
    """
    key = _clean_header(idempotency_key, "Idempotency-Key")
    corr = _clean_header(correlation_id, "X-Correlation-ID")
    try:
        pr, created = create_requisition(db, payload, idempotency_key=key, correlation_id=corr)
    except UnknownItem as exc:
        raise HTTPException(422, str(exc))
    except PastDeliveryDate as exc:
        raise HTTPException(422, str(exc))
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc))
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return pr


@router.post(
    "/{pr_id}/decision",
    response_model=DecisionResult,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": DecisionResult, "description": "Idempotent replay: the original decision"}},
)
def decide_pr(
    pr_id: int,
    payload: PRDecision,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Approve, reject or return a SUBMITTED PR. The server derives the approval level.

    The approver is user-supplied and NOT verified. The only rule checked is separation of duties by
    name (the approver may not be the requester); nothing proves the approver is a manager.
    """
    key = _clean_header(idempotency_key, "Idempotency-Key")
    corr = _clean_header(correlation_id, "X-Correlation-ID")
    try:
        approval, created = decide_requisition(db, pr_id, payload, idempotency_key=key, correlation_id=corr)
    except PRNotFound as exc:
        raise HTTPException(404, str(exc))
    except SeparationOfDuties as exc:
        raise HTTPException(403, str(exc))
    except (WrongState, DecisionConflict, IdempotencyConflict) as exc:
        raise HTTPException(409, str(exc))
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    pr = db.get(PurchaseRequisition, pr_id)
    return DecisionResult(
        **ApprovalRead.model_validate(approval).model_dump(),
        pr_number=pr.pr_number,
        pr_status=pr.status,
        levels_required=rules.required_approval_levels(pr),
    )


@router.post("/{pr_id}/resubmit", response_model=PRRead)
def resubmit_pr(
    pr_id: int,
    payload: PRCreate,
    db: Session = Depends(get_db),
    correlation_id: CorrelationHeader = None,
):
    """Send a RETURNED PR back for a new approval cycle. The body is the complete corrected PR."""
    corr = _clean_header(correlation_id, "X-Correlation-ID")
    try:
        return resubmit_requisition(db, pr_id, payload, correlation_id=corr)
    except PRNotFound as exc:
        raise HTTPException(404, str(exc))
    except NotTheRequester as exc:
        raise HTTPException(403, str(exc))
    except WrongState as exc:
        raise HTTPException(409, str(exc))
    except (UnknownItem, PastDeliveryDate) as exc:
        raise HTTPException(422, str(exc))


@router.get("/{pr_id}/approvals", response_model=list[ApprovalRead])
def list_approvals(pr_id: int, db: Session = Depends(get_db)):
    if db.get(PurchaseRequisition, pr_id) is None:
        raise HTTPException(404, "PR not found")
    return db.scalars(select(Approval).where(Approval.pr_id == pr_id).order_by(Approval.id)).all()


@router.get("/{pr_id}", response_model=PRRead)
def get_pr(pr_id: int, db: Session = Depends(get_db)):
    pr = db.get(PurchaseRequisition, pr_id)
    if pr is None:
        raise HTTPException(404, "PR not found")
    return pr


@router.get("", response_model=list[PRRead])
def list_prs(
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    db: Session = Depends(get_db),
):
    stmt = select(PurchaseRequisition)
    if status_filter is not None:
        stmt = stmt.where(PurchaseRequisition.status == status_filter)
    return db.scalars(stmt.order_by(PurchaseRequisition.id.desc()).limit(limit)).all()