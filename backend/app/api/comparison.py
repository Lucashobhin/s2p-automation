from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.schemas.comparison import ComparisonRead
from app.services.comparison import build_comparison
from app.services.rfqs import RFQError

router = APIRouter(tags=["comparison"])


@router.get("/rfq/{rfq_id}/comparison", response_model=ComparisonRead)
def compare(rfq_id: int, db: Session = Depends(get_db)):
    """The vendors' current quotations side by side. FACTS ONLY: no score, no rank, no winner.

    Only ACTIVE revisions. Coverage is quoted_quantity / requested_quantity. The lowest-price marker uses the
    TAX-INCLUSIVE price per unit; totals are only marked when every vendor quoted the same quantity.

    CONFIDENTIALITY: this shows vendor names and prices, so it is exactly as public as the quotation list.
    With SEAL_QUOTATIONS_UNTIL_DEADLINE on, EVERYONE gets 403 until the deadline has passed. Because the API
    has no authentication, this is NOT real buyer-only or vendor-only confidentiality.
    """
    try:
        return build_comparison(db, rfq_id)
    except RFQError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail())