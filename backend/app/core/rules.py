"""Business-rule constants. PLACEHOLDERS: the docs define none of these (see docs/ASSUMPTIONS.md)."""

SUPPORTED_CURRENCIES = ("INR",)   # POC: INR only. Add codes here when finance confirms others.
DEFAULT_CURRENCY = "INR"
MAX_PR_QUANTITY = 1_000_000       # also keeps the value inside SQLite's integer range
HEADER_TOKEN_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"  # allowed shape for Idempotency-Key / X-Correlation-ID