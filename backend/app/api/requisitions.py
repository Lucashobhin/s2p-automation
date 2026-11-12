import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.rules import HEADER_TOKEN_PATTERN
from app.db.database import get_db
from app.models.purchase_requisition import PurchaseRequisition
from app.schemas.requisition import PRCreate, PRRead
from app.services.requisitions import (
    IdempotencyConflict, PastDeliveryDate, UnknownItem, create_requisition,
)

router = APIRouter(prefix="/requisitions", tags=["requisitions"])
_TOKEN = re.compile(HEADER_TOKEN_PATTERN)


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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
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