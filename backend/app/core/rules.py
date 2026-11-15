"""Business-rule constants. PLACEHOLDERS: the docs define none of these (see docs/ASSUMPTIONS.md)."""

SUPPORTED_CURRENCIES = ("INR",)   # POC: INR only. Add codes here when finance confirms others.
DEFAULT_CURRENCY = "INR"
MAX_PR_QUANTITY = 1_000_000       # also keeps the value inside SQLite's integer range
HEADER_TOKEN_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"  # allowed shape for Idempotency-Key / X-Correlation-ID

DEFAULT_APPROVAL_LEVELS = 1       # A9: the docs say "multi-level" but give no levels or thresholds
MAX_COMMENT_LENGTH = 1000         # invented limit for approval comments
MAX_RFQ_VENDORS = 20              # invented limit: vendors invited to one RFQ
MAX_LEAD_TIME_DAYS = 365          # invented limit for a quotation's lead time (configurable)
# Sealed bids: when True, quotations cannot be read by ANYONE until the RFQ deadline has passed
# (or the RFQ is no longer PUBLISHED). Default off so the POC can be demonstrated; see A66.
SEAL_QUOTATIONS_UNTIL_DEADLINE = False


def required_approval_levels(pr) -> int:
    """How many approval levels this PR needs. PLACEHOLDER (A9, open for the senior).

    Takes the PR so real thresholds (for example by estimated_budget) can be added here later
    without touching the database or the callers.
    """
    return DEFAULT_APPROVAL_LEVELS