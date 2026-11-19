from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.core.enums import ApprovalDecision
from app.core.rules import MAX_COMMENT_LENGTH, MAX_JUSTIFICATION_LENGTH

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
QuotationId = Annotated[int, Field(strict=True, ge=1)]   # strict: no "5", no true (like D17)
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMMENT_LENGTH)]


def _clean_justification(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > MAX_JUSTIFICATION_LENGTH:
        raise ValueError(f"justification must be at most {MAX_JUSTIFICATION_LENGTH} characters")
    return value or None   # blank means "no justification"


def _clean_comments(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > MAX_COMMENT_LENGTH:
        raise ValueError(f"comments must be at most {MAX_COMMENT_LENGTH} characters")
    return value or None


# ---------- requests ----------

class QCSCreate(BaseModel):
    """Body of POST /api/rfq/{id}/qcs. The RFQ must be CLOSED."""

    model_config = ConfigDict(extra="forbid")

    created_by: Name = Field(
        description="Who creates the QCS. USER-SUPPLIED AND NOT VERIFIED: nothing proves who this is. "
        "Only the same name may edit or submit it, and it may not approve it."
    )
    recommended_quotation_id: QuotationId | None = Field(
        default=None, description="Optional now, required to submit. Must be an ACTIVE quotation of this RFQ."
    )
    justification: str | None = Field(default=None, description="Optional now, required to submit")

    _justification = field_validator("justification")(lambda cls, v: _clean_justification(v))


class QCSUpdate(BaseModel):
    """Body of PATCH /api/qcs/{id} (DRAFT only). Only the fields you SEND change; null clears one."""

    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description="Must be the QCS creator's name (a name check; identities are unverified)")
    recommended_quotation_id: QuotationId | None = None
    justification: str | None = None

    _justification = field_validator("justification")(lambda cls, v: _clean_justification(v))


class QCSSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description="Must be the QCS creator's name (a name check; identities are unverified)")


class QCSDecision(BaseModel):
    """Body of POST /api/qcs/{id}/decision. There is NO level field: the server derives it."""

    model_config = ConfigDict(extra="forbid")

    approver: Name = Field(
        description="Who is deciding. USER-SUPPLIED AND NOT VERIFIED (A40). Checked by name only: not the QCS "
        "creator, not the PR requester, and not someone who already decided a level of this QCS."
    )
    decision: ApprovalDecision = Field(description="APPROVED or REJECTED. RETURNED exists for PRs only.")
    comments: str | None = Field(default=None, description="Required for REJECTED")

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise_decision(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("comments")
    @classmethod
    def _comments(cls, value):
        return _clean_comments(value)

    @model_validator(mode="after")
    def _rules(self):
        if self.decision == ApprovalDecision.RETURNED:
            raise ValueError("RETURNED is not allowed for a QCS; use APPROVED or REJECTED")
        if self.decision == ApprovalDecision.REJECTED and not self.comments:
            raise ValueError("comments are required when rejecting a QCS")
        return self


class QCSWithdraw(BaseModel):
    """Body of POST /api/qcs/{id}/withdraw (APPROVED only, and only while no live PO exists)."""

    model_config = ConfigDict(extra="forbid")

    actor: Name = Field(description="Any named actor (open policy question, A81). Unverified.")
    reason: Reason


# ---------- responses ----------

class QCSRecommendation(BaseModel):
    """Facts about the recommended quotation. Money fields are strings in JSON."""

    quotation_id: int
    vendor_id: str
    vendor_name: str
    revision: int
    quoted_quantity: int
    coverage_percent: Decimal
    unit_price_incl_tax: Decimal   # total_amount / quoted_quantity, 4 decimals (D45)
    total_amount: Decimal          # tax included
    is_lowest_unit_price_incl_tax: bool
    is_partial: bool


class QCSRead(BaseModel):
    id: int
    rfq_id: int
    rfq_number: str
    rfq_status: str
    status: str
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    recommended_quotation_id: int | None
    justification: str | None
    recommendation: QCSRecommendation | None
    justification_min_length: int | None   # the rule that applies to this pick (20 or 80); null without a pick
    levels_required: int | None            # null without a pick
    levels_approved: int


class QCSApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    qcs_id: int | None
    level: int
    approval_cycle: int
    approver: str
    decision: str
    comments: str | None
    decided_at: datetime


class QCSDecisionResult(QCSApprovalRead):
    qcs_status: str      # the QCS status now (for a replay: its current status)
    levels_required: int