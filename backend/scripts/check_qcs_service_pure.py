"""Pure-function check for services/qcs.py block A. No database, no server.

Run from backend/:  python -m scripts.check_qcs_service_pure
"""
from types import SimpleNamespace as N

from app.core import rules
from app.schemas.qcs import QCSCreate
from app.services import qcs as svc

passed = failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label:<78} {extra}")


print("--- errors: status code + machine-readable detail (D61)")
e = svc.QCSConflict("RFQ_NOT_CLOSED", "msg", rfq_status="PUBLISHED")
check("detail() = code + message + the extra state", e.detail() == {
    "code": "RFQ_NOT_CLOSED", "message": "msg", "rfq_status": "PUBLISHED"})
check("not found is 404", svc.QCSNotFound("X", "m").status_code == 404)
check("forbidden is 403", svc.QCSForbidden("X", "m").status_code == 403)
check("conflict is 409", svc.QCSConflict("X", "m").status_code == 409)
check("invalid is 422", svc.QCSInvalid("X", "m").status_code == 422)

print("--- create hash (idempotency)")
a = QCSCreate(created_by="Asha", recommended_quotation_id=3, justification="because")
b = QCSCreate(created_by="  Asha ", recommended_quotation_id=3, justification=" because ")
check("same content (even untrimmed input) -> same hash", svc._create_hash(a) == svc._create_hash(b))
check("different justification -> different hash",
      svc._create_hash(a) != svc._create_hash(QCSCreate(created_by="Asha", recommended_quotation_id=3, justification="other")))
check("different quotation -> different hash",
      svc._create_hash(a) != svc._create_hash(QCSCreate(created_by="Asha", recommended_quotation_id=4, justification="because")))
check("different creator -> different hash",
      svc._create_hash(a) != svc._create_hash(QCSCreate(created_by="Bina", recommended_quotation_id=3, justification="because")))
check("no pick and no justification still hashes", len(svc._create_hash(QCSCreate(created_by="A"))) == 64)

print("--- D50: justification rule")
lowest_full = N(is_lowest_unit_price_incl_tax=True, is_partial=False)
not_lowest = N(is_lowest_unit_price_incl_tax=False, is_partial=False)
partial = N(is_lowest_unit_price_incl_tax=True, is_partial=True)
both = N(is_lowest_unit_price_incl_tax=False, is_partial=True)
check("lowest + full coverage -> normal minimum, no reasons",
      svc.justification_rule(lowest_full) == (rules.MIN_JUSTIFICATION_LENGTH, []))
check("not the lowest -> strict minimum, NOT_LOWEST_UNIT_PRICE",
      svc.justification_rule(not_lowest) == (rules.MIN_JUSTIFICATION_LENGTH_STRICT, ["NOT_LOWEST_UNIT_PRICE"]))
check("partial quote -> strict minimum, PARTIAL_QUOTE",
      svc.justification_rule(partial) == (rules.MIN_JUSTIFICATION_LENGTH_STRICT, ["PARTIAL_QUOTE"]))
check("not lowest AND partial -> strict minimum, both reasons",
      svc.justification_rule(both) == (rules.MIN_JUSTIFICATION_LENGTH_STRICT, ["NOT_LOWEST_UNIT_PRICE", "PARTIAL_QUOTE"]))
check("strict is stricter than normal", rules.MIN_JUSTIFICATION_LENGTH_STRICT > rules.MIN_JUSTIFICATION_LENGTH)

print("--- D53: approval levels")
check("a QCS needs 1 level for now (placeholder, A9)", rules.required_qcs_approval_levels(N(total_amount=1)) == 1)

print("--- helpers")
check("name comparison ignores case and spaces", svc._norm("  Asha ") == svc._norm("ASHA"))

print(f"\n{passed}/{passed + failed} checks passed")
print("ALL PASS" if failed == 0 else "SOME FAILED")