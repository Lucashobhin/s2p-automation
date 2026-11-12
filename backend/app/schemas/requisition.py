from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.rules import DEFAULT_CURRENCY, MAX_PR_QUANTITY, SUPPORTED_CURRENCIES

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ItemId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class PRCreate(BaseModel):
    """Body of POST /api/requisitions. Unknown fields are rejected (no injecting status etc.)."""

    model_config = ConfigDict(extra="forbid")

    requester: Name = Field(
        description="Who is raising the PR. USER-SUPPLIED AND NOT VERIFIED: there is no login yet "
        "(A14, A40). Also recorded as the audit actor."
    )
    department: Name
    item_id: ItemId
    quantity: int = Field(strict=True, ge=1, le=MAX_PR_QUANTITY, description="Whole number of units")
    estimated_budget: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default=DEFAULT_CURRENCY)
    required_delivery_date: date = Field(description="ISO date string such as 2026-12-31, today or later")

    @field_validator("required_delivery_date", mode="before")
    @classmethod
    def _iso_string_only(cls, value):
        # Accept ISO date strings only: a bare number would otherwise be read as a timestamp.
        if isinstance(value, date) or isinstance(value, str):
            return value
        raise ValueError("must be an ISO date string such as 2026-12-31")

    @field_validator("currency")
    @classmethod
    def _supported_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency; supported: {', '.join(SUPPORTED_CURRENCIES)}")
        return value


class PRRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pr_number: str
    requester: str
    department: str
    item_id: str
    quantity: int
    ordered_quantity: int
    estimated_budget: Decimal   # serialised as a string, e.g. "50000.10"
    currency: str
    required_delivery_date: date
    status: str
    created_at: datetime