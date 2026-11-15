from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.headers import CorrelationHeader, IdempotencyKeyHeader, clean_header
from app.db.database import get_db
from app.schemas.quotation import QuotationRead, QuotationSubmit
from app.services.quotations import get_quotation, list_quotations, submit_quotation
from app.services.rfqs import RFQError

router = APIRouter(tags=["quotations"])


def _raise(exc: RFQError):
    raise HTTPException(exc.status_code, detail=exc.detail())


@router.post(
    "/rfq/{rfq_id}/quotations",
    response_model=QuotationRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": QuotationRead, "description": "Idempotent replay, or a resubmission identical to "
                     "the vendor's current quotation: the existing quotation, nothing created"}},
)
def submit(
    rfq_id: int,
    payload: QuotationSubmit,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Submit a quotation, or REVISE it until the response deadline (each revision supersedes the last).

    SECURITY LIMIT: `vendor_id` is NOT verified. Anyone can submit on behalf of any INVITED vendor until the
    vendor portal has logins (Phase 8). The server computes all money; client totals are rejected.
    After the deadline nothing new is accepted; only a retry carrying a known Idempotency-Key still returns
    the original result. The Idempotency-Key is scoped to RFQ + vendor.
    """
    key = clean_header(idempotency_key, "Idempotency-Key")
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        quotation, created = submit_quotation(db, rfq_id, payload, idempotency_key=key, correlation_id=corr)
    except RFQError as exc:
        _raise(exc)
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return quotation


@router.get("/rfq/{rfq_id}/quotations", response_model=list[QuotationRead])
def list_rfq_quotations(
    rfq_id: int,
    include_history: Annotated[bool, Query(description="Also return SUPERSEDED revisions")] = False,
    db: Session = Depends(get_db),
):
    """The vendors' current (ACTIVE) quotations for an RFQ; `include_history=true` adds older revisions.

    CONFIDENTIALITY: by default anyone can read this at any time, so competitors' prices are visible.
    With SEAL_QUOTATIONS_UNTIL_DEADLINE on, EVERYONE gets 403 QUOTATIONS_SEALED until the deadline has
    passed (or the RFQ leaves PUBLISHED); there is no authentication to tell a buyer from a competitor.
    """
    try:
        return list_quotations(db, rfq_id, include_history=include_history)
    except RFQError as exc:
        _raise(exc)


@router.get("/quotations/{quotation_id}", response_model=QuotationRead)
def get_one_quotation(quotation_id: int, db: Session = Depends(get_db)):
    """One quotation (any revision). Subject to the same sealing rule as the list."""
    try:
        return get_quotation(db, quotation_id)
    except RFQError as exc:
        _raise(exc)