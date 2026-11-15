"""Status values. PLACEHOLDER: the docs define none. See docs/ASSUMPTIONS.md (A12, A28)."""
from enum import Enum


class PRStatus(str, Enum):
    SUBMITTED = "SUBMITTED"      # created, waiting for approval
    APPROVED = "APPROVED"        # approved, ready for RFQ
    REJECTED = "REJECTED"        # approver said no (terminal)
    RETURNED = "RETURNED"        # approver sent it back for changes; requester resubmits
    RFQ_CREATED = "RFQ_CREATED"  # RFQ generated from this PR
    CANCELLED = "CANCELLED"      # withdrawn by requester (terminal)


class RFQStatus(str, Enum):
    DRAFT = "DRAFT"          # auto-created from an approved PR, not yet sent
    PUBLISHED = "PUBLISHED"  # sent to invited vendors
    CLOSED = "CLOSED"        # deadline passed, evaluating quotations
    AWARDED = "AWARDED"      # a quotation was chosen
    CANCELLED = "CANCELLED"


class QuotationStatus(str, Enum):
    ACTIVE = "ACTIVE"          # the vendor's current quotation (at most one per RFQ and vendor)
    SUPERSEDED = "SUPERSEDED"  # replaced by a later revision; kept as history
    WITHDRAWN = "WITHDRAWN"    # reserved: no withdraw endpoint yet


class QCSStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class POStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ERP_PUSHED = "ERP_PUSHED"  # mock SAP accepted it and returned a reference
    CANCELLED = "CANCELLED"


class ApprovalDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"   # final: no new approval cycle after it
    RETURNED = "RETURNED"   # PR only (the docs say a manager may "return" a PR); opens a new cycle


# PO statuses whose quantity counts against the PR (see A25, A30).
ACTIVE_PO_STATUSES = (
    POStatus.PENDING_APPROVAL.value,
    POStatus.APPROVED.value,
    POStatus.ERP_PUSHED.value,
)