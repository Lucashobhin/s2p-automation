from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.headers import CorrelationHeader, IdempotencyKeyHeader, clean_header
from app.db.database import get_db
from app.schemas.qcs import (
    QCSApprovalRead, QCSCreate, QCSDecision, QCSDecisionResult, QCSRead, QCSSubmit, QCSUpdate,
)
from app.services.qcs import (
    QCSError, create_qcs, decide_qcs, get_qcs, get_qcs_for_rfq, list_approvals, submit_qcs,
    to_decision_result, to_read, update_qcs,
)

router = APIRouter(tags=["qcs"])


def _raise(exc: QCSError):
    raise HTTPException(exc.status_code, detail=exc.detail())


@router.post(
    "/rfq/{rfq_id}/qcs",
    response_model=QCSRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": QCSRead, "description": "Idempotent replay: the original QCS"}},
)
def create(
    rfq_id: int,
    payload: QCSCreate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Create a DRAFT QCS for a CLOSED RFQ (one per RFQ). created_by is unverified.

    A replay (same Idempotency-Key and same body) returns the original QCS even if the RFQ has since changed.
    Errors are `{"code", "message", ...state}`: read the code, then GET the QCS before deciding what to do.
    """
    key = clean_header(idempotency_key, "Idempotency-Key")
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        qcs, created = create_qcs(db, rfq_id, payload, idempotency_key=key, correlation_id=corr)
    except QCSError as exc:
        _raise(exc)
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return to_read(db, qcs)


@router.get("/rfq/{rfq_id}/qcs", response_model=QCSRead)
def get_for_rfq(rfq_id: int, db: Session = Depends(get_db)):
    """The QCS of an RFQ (404 QCS_NOT_FOUND if it has none yet)."""
    try:
        return to_read(db, get_qcs_for_rfq(db, rfq_id))
    except QCSError as exc:
        _raise(exc)


@router.get("/qcs/{qcs_id}", response_model=QCSRead)
def get_one(qcs_id: int, db: Session = Depends(get_db)):
    try:
        return to_read(db, get_qcs(db, qcs_id))
    except QCSError as exc:
        _raise(exc)


@router.patch("/qcs/{qcs_id}", response_model=QCSRead)
def update(qcs_id: int, payload: QCSUpdate, db: Session = Depends(get_db), correlation_id: CorrelationHeader = None):
    """Edit a DRAFT QCS. Only the fields you SEND change; null clears one. Only the creator's name may edit
    (a name check; identities are unverified). Nothing to change = 200 with no write and no audit row.

    Check order: 404, 403 NOT_THE_CREATOR, 409 QCS_NOT_DRAFT, 409 RFQ_NOT_CLOSED, then 422 content rules.
    A race returns the code of the state that actually won (for example RFQ_NOT_CLOSED or QCS_CHANGED).
    """
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        return to_read(db, update_qcs(db, qcs_id, payload, correlation_id=corr))
    except QCSError as exc:
        _raise(exc)


@router.post("/qcs/{qcs_id}/submit", response_model=QCSRead)
def submit(qcs_id: int, payload: QCSSubmit, db: Session = Depends(get_db), correlation_id: CorrelationHeader = None):
    """Submit a DRAFT QCS for approval (DRAFT -> PENDING_APPROVAL). Creator only (a name check).

    No Idempotency-Key: a repeat gets 409 QCS_NOT_DRAFT with the current status; GET the QCS to see whether
    the first request worked. The justification must be at least 20 characters, or 80 when the pick is not the
    lowest tax-inclusive unit price or is a partial quote (a speed bump, not proof of a good decision).
    """
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        return to_read(db, submit_qcs(db, qcs_id, payload, correlation_id=corr))
    except QCSError as exc:
        _raise(exc)




@router.post(
    "/qcs/{qcs_id}/decision",
    response_model=QCSDecisionResult,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": QCSDecisionResult, "description": "Idempotent replay: the original approval"}},
)
def decide(
    qcs_id: int,
    payload: QCSDecision,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: IdempotencyKeyHeader = None,
    correlation_id: CorrelationHeader = None,
):
    """Approve or reject a QCS that is PENDING_APPROVAL. There is NO level field: the server derives it.

    The approver is unverified. Names are checked: not the QCS creator, not the PR requester, and not someone
    who already decided a level of this QCS. REJECTED is final at any level; APPROVED below the last level
    keeps the QCS PENDING_APPROVAL. Approving does not award the RFQ or create a PO (Slice 3g).

    A replay (same Idempotency-Key, same approver, same body) returns the original approval with the QCS's
    CURRENT status. The same key from another approver or with another body is 409 IDEMPOTENCY_KEY_REUSED.
    Check order: replay, 404, 403, 409 QCS_NOT_PENDING, 409 RFQ_NOT_CLOSED. A race returns the code of the
    state that actually won.
    """
    key = clean_header(idempotency_key, "Idempotency-Key")
    corr = clean_header(correlation_id, "X-Correlation-ID")
    try:
        approval, created = decide_qcs(db, qcs_id, payload, idempotency_key=key, correlation_id=corr)
        result = to_decision_result(db, approval)
    except QCSError as exc:
        _raise(exc)
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return result



@router.get("/qcs/{qcs_id}/approvals", response_model=list[QCSApprovalRead])
def approvals(qcs_id: int, db: Session = Depends(get_db)):
    """Every decision on this QCS, in level order (history rows, never edited)."""
    try:
        return list_approvals(db, qcs_id)
    except QCSError as exc:
        _raise(exc)