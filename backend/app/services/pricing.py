"""Quotation amount rules (see docs/ASSUMPTIONS.md, D4).

    net_amount   = quantity * unit_price            (unit_price is net, excludes tax)
    tax_amount   = net_amount * tax_rate_percent / 100, rounded HALF-UP to 2 decimals
    total_amount = net_amount + tax_amount          (tax included)

Discounts, freight and other charges are out of scope. Inputs must be Decimal,
never float: floats cannot represent money exactly.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


@dataclass(frozen=True)
class QuotationAmounts:
    net_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def compute_quotation_amounts(
    quantity: int, unit_price: Decimal, tax_rate_percent: Decimal
) -> QuotationAmounts:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise TypeError("quantity must be an int")
    if not isinstance(unit_price, Decimal) or not isinstance(tax_rate_percent, Decimal):
        raise TypeError("unit_price and tax_rate_percent must be Decimal, not float")
    if quantity <= 0:
        raise ValueError("quantity must be greater than 0")
    if unit_price < 0:
        raise ValueError("unit_price must not be negative")
    if not (Decimal("0") <= tax_rate_percent <= Decimal("100")):
        raise ValueError("tax_rate_percent must be between 0 and 100")

    net = _money(unit_price * quantity)
    tax = _money(net * tax_rate_percent / Decimal("100"))
    return QuotationAmounts(net_amount=net, tax_amount=tax, total_amount=net + tax)