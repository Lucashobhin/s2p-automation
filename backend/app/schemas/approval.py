from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.core.enums import ApprovalDecision
from app.core.rules import MAX_COMMENT_LENGTH

Approver = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class PRDecision(BaseModel):
    """Body of POST /api/requisitions/{id}/decision. There is NO level field: the server derives it."""

    model_config = ConfigDict(extra="forbid")

    approver: Approver = Field(
        description="Who is deciding. USER-SUPPLIED AND NOT VERIFIED: nothing proves this person is a "
        "manager (A40). The only rule checked is that it is not the requester's name."
    )
    decision: ApprovalDecision
    comments: str | None = Field(default=None, description="Required for REJECTED and RETURNED")

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise_decision(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("comments")
    @classmethod
    def _clean_comments(cls, value):
        if value is None:
            return None
        value = value.strip()
        if len(value) > MAX_COMMENT_LENGTH:
            raise ValueError(f"comments must be at most {MAX_COMMENT_LENGTH} characters")
        return value or None

    @model_validator(mode="after")
    def _comments_required(self):
        if self.decision in (ApprovalDecision.REJECTED, ApprovalDecision.RETURNED) and not self.comments:
            raise ValueError("comments are required when rejecting or returning a PR")
        return self


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pr_id: int | None
    level: int
    approval_cycle: int
    approver: str
    decision: str
    comments: str | None
    decided_at: datetime


class DecisionResult(ApprovalRead):
    pr_number: str
    pr_status: str       # the PR's status now (for a replay: its current status)
    levels_required: int