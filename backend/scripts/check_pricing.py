"""Check the quotation amount rules (D4). Run from backend/:  python -m scripts.check_pricing"""
from decimal import Decimal as D

from app.services.pricing import compute_quotation_amounts

passed = failed = 0


def expect(label, got, want):
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label:34} got={got} want={want}")


def expect_error(label, exc_type, *args):
    global passed, failed
    try:
        compute_quotation_amounts(*args)
        ok, got = False, "no error"
    except exc_type as e:
        ok, got = True, f"{exc_type.__name__}"
    passed += ok
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label:34} -> {got}")


a = compute_quotation_amounts(5, D("4200.00"), D("18.00"))
expect("5 x 4200.00 @18% net", a.net_amount, D("21000.00"))
expect("5 x 4200.00 @18% tax", a.tax_amount, D("3780.00"))
expect("5 x 4200.00 @18% total", a.total_amount, D("24780.00"))

a = compute_quotation_amounts(3, D("33.33"), D("18.00"))
expect("3 x 33.33 @18% tax (17.9982)", a.tax_amount, D("18.00"))
expect("3 x 33.33 @18% total", a.total_amount, D("117.99"))

a = compute_quotation_amounts(1, D("0.25"), D("10.00"))
expect("half-up: tax 0.025 -> 0.03", a.tax_amount, D("0.03"))   # banker's rounding would give 0.02

a = compute_quotation_amounts(2, D("100.00"), D("0"))
expect("0% tax: total = net", a.total_amount, D("200.00"))

expect_error("float price rejected", TypeError, 5, 4200.0, D("18"))
expect_error("float tax rejected", TypeError, 5, D("4200"), 18.0)
expect_error("quantity 0 rejected", ValueError, 0, D("1"), D("18"))
expect_error("negative price rejected", ValueError, 1, D("-1"), D("18"))
expect_error("tax 101% rejected", ValueError, 1, D("1"), D("101"))
expect_error("bool quantity rejected", TypeError, True, D("1"), D("18"))

print(f"\n{passed} passed, {failed} failed")
print("ALL PASS" if failed == 0 else "SOMETHING FAILED")