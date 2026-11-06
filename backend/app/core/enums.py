"""Status values. PLACEHOLDER: the docs define none. See docs/ASSUMPTIONS.md (A12)."""
from enum import Enum


class PRStatus(str, Enum):
    SUBMITTED = "SUBMITTED"      # created, waiting for approval
    APPROVED = "APPROVED"        # approved, ready for RFQ
    REJECTED = "REJECTED"        # approver said no (terminal)
    RFQ_CREATED = "RFQ_CREATED"  # RFQ generated from this PR
    CANCELLED = "CANCELLED"      # withdrawn by requester (terminal)
