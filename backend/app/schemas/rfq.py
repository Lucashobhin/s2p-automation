from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.rules import MAX_COMMENT_LENGTH, MAX_RFQ_VENDORS
from app.schemas.requisition import Name

VendorIdStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
_ACTOR_NOTE = "USER-SUPPLIED AND NOT VERIFIED: there is no login yet (A40)."


class RFQGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_id: int = Field(strict=True, ge=1, le=2_147_483_647)
    actor: Name = Field(description=f"Who is generating the RFQ. {_ACTOR_NOTE}")


class RFQPublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description=f"Who is publishing. {_ACTOR_NOTE}")
    vendor_ids: list[VendorIdStr] = Field(min_length=1, max_length=MAX_RFQ_VENDORS)
    response_deadline: date = Field(description="ISO date string; after today and not after the PR's delivery date")

    @field_validator("response_deadline", mode="before")
    @classmethod
    def _iso_string_only(cls, value):
        if isinstance(value, (date, str)):
            return value
        raise ValueError("must be an ISO date string such as 2026-12-31")


class RFQCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description=f"Who is cancelling. {_ACTOR_NOTE}")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMMENT_LENGTH)]


class RFQClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description=f"Who is closing the RFQ. {_ACTOR_NOTE}")


class RFQVendorRead(BaseModel):
    vendor_id: str
    vendor_name: str
    oem_approved: bool
    risk_flags: list[str]   # NON_OEM is a flag, not a block (A7)


class RFQRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rfq_number: str
    pr_id: int
    round_number: int
    status: str
    requested_quantity: int
    issue_date: date | None
    response_deadline: date | None
    created_at: datetime
    vendors: list[RFQVendorRead] = []