from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core import rules
from app.core.rules import MAX_PR_QUANTITY, SUPPORTED_CURRENCIES

VendorIdStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class QuotationSubmit(BaseModel):
    """Body of POST /api/rfq/{id}/quotations.

    There are NO total fields: the server computes net, tax and total (Decimal, tax rounded half-up).
    A client-sent total is rejected, never trusted.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_id: VendorIdStr = Field(
        description="The quoting vendor. USER-SUPPLIED AND NOT VERIFIED until the vendor portal has "
        "logins (Phase 8): anyone can submit a quotation on behalf of any INVITED vendor (A66)."
    )
    quoted_quantity: int = Field(strict=True, ge=1, le=MAX_PR_QUANTITY, description="At most the RFQ's requested quantity")
    unit_price: Decimal = Field(ge=0, max_digits=14, decimal_places=2, description="Net price per unit, excluding tax")
    tax_rate_percent: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    currency: str = Field(default="INR")
    lead_time_days: int = Field(strict=True, description="Whole days; the maximum is core/rules.py MAX_LEAD_TIME_DAYS")
    payment_terms: str | None = Field(default=None)
    source_document_ref: str | None = Field(default=None, description="Where the quote came from, e.g. a PDF name")

    @field_validator("currency")
    @classmethod
    def _supported_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency; supported: {', '.join(SUPPORTED_CURRENCIES)}")
        return value

    @field_validator("lead_time_days")
    @classmethod
    def _lead_time_range(cls, value: int) -> int:
        # Read at validation time so the limit stays configurable in core/rules.py.
        if not 0 <= value <= rules.MAX_LEAD_TIME_DAYS:
            raise ValueError(f"lead_time_days must be between 0 and {rules.MAX_LEAD_TIME_DAYS}")
        return value

    @field_validator("payment_terms")
    @classmethod
    def _payment_terms(cls, value):
        value = value.strip() if value is not None else None
        if value and len(value) > 50:
            raise ValueError("payment_terms must be at most 50 characters")
        return value or None

    @field_validator("source_document_ref")
    @classmethod
    def _source_ref(cls, value):
        value = value.strip() if value is not None else None
        if value and len(value) > 255:
            raise ValueError("source_document_ref must be at most 255 characters")
        return value or None


class QuotationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rfq_id: int
    vendor_id: str
    revision: int
    status: str            # ACTIVE = the vendor's current quotation; SUPERSEDED = an older revision
    quoted_quantity: int
    unit_price: Decimal    # money is serialised as strings, e.g. "4200.00"
    tax_rate_percent: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal  # tax included
    currency: str
    lead_time_days: int
    payment_terms: str | None
    source_document_ref: str | None
    created_at: datetime