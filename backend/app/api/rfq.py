from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.headers import CorrelationHeader, IdempotencyKeyHeader, clean_header
from app.core.enums import RFQStatus
from app.db.database import get_db
from app.models.rfq import RFQ
from app.schemas.rfq import RFQCancel, RFQClose, RFQGenerate, RFQPublish, RFQRead
from app.services.rfqs import RFQError, cancel_rfq, close_rfq, generate_rfq, publish_rfq, to_read

router = APIRouter(prefix="/rfq", tags=["rfq"])


def _raise(exc: RFQError):
    raise HTTPException(exc.status_code, detail=exc.detail())


@router.post(
    "/generate",
    response_model=RFQRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RFQRead, "description": "Idempotent replay: the original RFQ"}},
)
def generate(
    payload: RFQGenerate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Create a DRAFT RFQ for an APPROVED PR's remaining quantity (actor is unverified).

    Errors are `{"code", "message", ...state}`. A 409 does NOT always mean "already done": read the
    code (and GET the RFQ) before deciding.
    """
    key = clean_header(idempotency_key, "Idempotency-Key")
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        rfq, created = generate_rfq(db, payload.pr_id, payload.actor, idempotency_key=key, correlation_id=corr)
    except RFQError as exc:
        _raise(exc)
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return to_read(db, rfq)


@router.post("/{rfq_id}/publish", response_model=RFQRead)
def publish(
    rfq_id: int, payload: RFQPublish, db: Session = Depends(get_db), correlation_id: CorrelationHeader = None,
):
    """Record the vendor invitations and issue the RFQ. Does NOT notify vendors (n8n does, Phase 7)."""
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        return to_read(db, publish_rfq(db, rfq_id, payload, correlation_id=corr))
    except RFQError as exc:
        _raise(exc)


@router.post("/{rfq_id}/close", response_model=RFQRead)
def close(
    rfq_id: int, payload: RFQClose, db: Session = Depends(get_db), correlation_id: CorrelationHeader = None,
):
    """Close a PUBLISHED RFQ after its response deadline so the quotations can be evaluated.

    Needs at least one ACTIVE quotation (otherwise cancel). No early close. n8n calls this after the
    deadline: nothing here runs by itself. A repeat is 409 `RFQ_ALREADY_CLOSED` with the status.
    """
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        return to_read(db, close_rfq(db, rfq_id, payload, correlation_id=corr))
    except RFQError as exc:
        _raise(exc)


@router.post("/{rfq_id}/cancel", response_model=RFQRead)
def cancel(
    rfq_id: int, payload: RFQCancel, db: Session = Depends(get_db), correlation_id: CorrelationHeader = None,
):
    """Cancel an RFQ so the buyer can re-tender.

    DRAFT: always. PUBLISHED: only with no quotations (otherwise close it first). CLOSED: allowed even
    with quotations (kept as history), but refused while a QCS is PENDING_APPROVAL or APPROVED or an
    active PO exists on one of its quotations. Errors carry a `code` and the current status.
    """
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        return to_read(db, cancel_rfq(db, rfq_id, payload, correlation_id=corr))
    except RFQError as exc:
        _raise(exc)


@router.get("/active", response_model=list[RFQRead])
def active_rfqs(limit: Annotated[int, Query(ge=1, le=200)] = 50, db: Session = Depends(get_db)):
    """RFQs currently PUBLISHED (the docs' GET /api/rfq/active)."""
    rows = db.scalars(
        select(RFQ).where(RFQ.status == RFQStatus.PUBLISHED.value).order_by(RFQ.id.desc()).limit(limit)
    ).all()
    return [to_read(db, r) for r in rows]


@router.get("/{rfq_id}", response_model=RFQRead)
def get_rfq(rfq_id: int, db: Session = Depends(get_db)):
    rfq = db.get(RFQ, rfq_id)
    if rfq is None:
        raise HTTPException(404, detail={"code": "RFQ_NOT_FOUND", "message": f"RFQ {rfq_id} not found"})
    return to_read(db, rfq)