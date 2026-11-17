from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class ComparisonRow(BaseModel):
    vendor_id: str
    vendor_name: str
    oem_approved: bool
    risk_flags: list[str]          # NON_OEM is a flag, not a block (A7)
    quotation_id: int
    revision: int
    quoted_quantity: int
    coverage_percent: Decimal      # quoted_quantity / requested_quantity x 100, e.g. "20.00"
    full_coverage: bool
    unit_price: Decimal            # net, excluding tax, as quoted
    tax_rate_percent: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal          # tax included; NOT comparable when quantities differ
    unit_price_incl_tax: Decimal   # total_amount / quoted_quantity, 4 decimals: the like-for-like figure
    lead_time_days: int
    payment_terms: str | None
    is_lowest_unit_price_incl_tax: bool
    is_shortest_lead_time: bool
    is_lowest_total: bool          # only ever true when totals_comparable is true


class ComparisonRead(BaseModel):
    """FACTS ONLY. There is no score, no rank and no winner anywhere in this response."""

    rfq_id: int
    rfq_number: str
    rfq_status: str
    pr_id: int
    item_id: str
    item_name: str
    currency: str
    requested_quantity: int
    response_deadline: date | None
    quotation_count: int
    totals_comparable: bool | None   # None when there are fewer than two quotations to compare
    totals_note: str | None
    ordering: str
    rows: list[ComparisonRow]