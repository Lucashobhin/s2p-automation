"""Check for app/schemas/qcs.py. No database, no server.

Run from backend/:  python -m scripts.check_qcs_schemas
"""
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from app.core.rules import MAX_COMMENT_LENGTH, MAX_JUSTIFICATION_LENGTH
from app.schemas.qcs import (
    QCSApprovalRead,
    QCSCreate,
    QCSDecision,
    QCSRead,
    QCSRecommendation,
    QCSSubmit,
    QCSUpdate,
    QCSWithdraw,
)

passed = failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<78} {extra}")


def refused(label: str, fn) -> None:
    try:
        fn()
    except ValidationError:
        check(label, True)
        return
    except Exception as exc:  # any other error type is a bug in the schema
        check(label, False, f"wrong error: {type(exc).__name__}")
        return
    check(label, False, "was ACCEPTED")


print("--- QCSCreate")
c = QCSCreate(created_by="  Asha  ")
check("created_by is trimmed", c.created_by == "Asha")
check("quotation and justification are optional (None)", c.recommended_quotation_id is None and c.justification is None)
refused("created_by missing -> refused", lambda: QCSCreate())
refused("created_by empty -> refused", lambda: QCSCreate(created_by=""))
refused("created_by whitespace only -> refused", lambda: QCSCreate(created_by="   "))
refused("created_by over 100 characters -> refused", lambda: QCSCreate(created_by="x" * 101))
refused("extra field -> refused", lambda: QCSCreate(created_by="A", status="APPROVED"))
check("blank justification becomes None", QCSCreate(created_by="A", justification="   ").justification is None)
check("justification is trimmed", QCSCreate(created_by="A", justification="  ok  ").justification == "ok")
check(
    "justification at the maximum is accepted",
    len(QCSCreate(created_by="A", justification="x" * MAX_JUSTIFICATION_LENGTH).justification)
    == MAX_JUSTIFICATION_LENGTH,
)
refused(
    "justification over the maximum -> refused",
    lambda: QCSCreate(created_by="A", justification="x" * (MAX_JUSTIFICATION_LENGTH + 1)),
)
check("quotation id 7 accepted", QCSCreate(created_by="A", recommended_quotation_id=7).recommended_quotation_id == 7)
refused("quotation id 0 -> refused", lambda: QCSCreate(created_by="A", recommended_quotation_id=0))
refused("quotation id as text '5' -> refused (strict)", lambda: QCSCreate(created_by="A", recommended_quotation_id="5"))
refused("quotation id true -> refused (strict)", lambda: QCSCreate(created_by="A", recommended_quotation_id=True))

print("--- QCSUpdate (only the fields SENT change)")
u = QCSUpdate(actor="Asha")
check(
    "nothing sent: neither field is 'set'",
    "justification" not in u.model_fields_set and "recommended_quotation_id" not in u.model_fields_set,
)
u = QCSUpdate(actor="Asha", justification=None)
check("explicit null is 'set' (it clears the field)", "justification" in u.model_fields_set and u.justification is None)
u = QCSUpdate(actor="Asha", recommended_quotation_id=3)
check(
    "only the quotation sent: justification not 'set'",
    "recommended_quotation_id" in u.model_fields_set and "justification" not in u.model_fields_set,
)
refused("actor missing -> refused", lambda: QCSUpdate(justification="x"))
refused("actor blank -> refused", lambda: QCSUpdate(actor=" "))
refused("extra field -> refused", lambda: QCSUpdate(actor="A", status="DRAFT"))
refused(
    "justification over the maximum -> refused",
    lambda: QCSUpdate(actor="A", justification="x" * (MAX_JUSTIFICATION_LENGTH + 1)),
)

print("--- QCSSubmit")
check("actor is trimmed", QCSSubmit(actor="  Asha ").actor == "Asha")
refused("actor blank -> refused", lambda: QCSSubmit(actor=""))
refused("extra field -> refused", lambda: QCSSubmit(actor="A", note="x"))

print("--- QCSDecision")
check("'approved ' is normalised to APPROVED", QCSDecision(approver="B", decision="approved ").decision.value == "APPROVED")
check("APPROVED without comments is fine", QCSDecision(approver="B", decision="APPROVED").comments is None)
check("REJECTED with comments is fine", QCSDecision(approver="B", decision="REJECTED", comments=" too dear ").comments == "too dear")
refused("REJECTED without comments -> refused", lambda: QCSDecision(approver="B", decision="REJECTED"))
refused("REJECTED with blank comments -> refused", lambda: QCSDecision(approver="B", decision="REJECTED", comments="  "))
refused("RETURNED -> refused (PR only)", lambda: QCSDecision(approver="B", decision="RETURNED", comments="x"))
refused("unknown decision -> refused", lambda: QCSDecision(approver="B", decision="MAYBE"))
refused(
    "comments over the maximum -> refused",
    lambda: QCSDecision(approver="B", decision="APPROVED", comments="x" * (MAX_COMMENT_LENGTH + 1)),
)
refused("approver blank -> refused", lambda: QCSDecision(approver=" ", decision="APPROVED"))
refused("a client-chosen level -> refused (no such field)", lambda: QCSDecision(approver="B", decision="APPROVED", level=2))

print("--- QCSWithdraw")
check("reason is trimmed", QCSWithdraw(actor="A", reason="  wrong vendor ").reason == "wrong vendor")
refused("reason missing -> refused", lambda: QCSWithdraw(actor="A"))
refused("reason blank -> refused", lambda: QCSWithdraw(actor="A", reason="  "))
refused(
    "reason over the maximum -> refused",
    lambda: QCSWithdraw(actor="A", reason="x" * (MAX_COMMENT_LENGTH + 1)),
)
refused("actor missing -> refused", lambda: QCSWithdraw(reason="x"))

print("--- responses")
now = datetime(2026, 1, 1, tzinfo=timezone.utc)
rec = QCSRecommendation(
    quotation_id=1,
    vendor_id="V001",
    vendor_name="Vendor One",
    revision=1,
    quoted_quantity=100,
    coverage_percent=Decimal("100.00"),
    unit_price_incl_tax=Decimal("4720.0000"),
    total_amount=Decimal("472000.00"),
    is_lowest_unit_price_incl_tax=True,
    is_partial=False,
)
read = QCSRead(
    id=1,
    rfq_id=2,
    rfq_number="RFQ-0002",
    rfq_status="CLOSED",
    status="DRAFT",
    created_by="Asha",
    created_at=now,
    updated_at=now,
    recommended_quotation_id=1,
    justification=None,
    recommendation=rec,
    justification_min_length=20,
    levels_required=1,
    levels_approved=0,
)
data = read.model_dump(mode="json")
check(
    "money is serialised as STRINGS",
    data["recommendation"]["total_amount"] == "472000.00"
    and data["recommendation"]["unit_price_incl_tax"] == "4720.0000"
    and data["recommendation"]["coverage_percent"] == "100.00",
)
check(
    "no-pick QCS: recommendation / min length / levels_required may be null",
    QCSRead(
        id=1,
        rfq_id=2,
        rfq_number="R",
        rfq_status="CLOSED",
        status="DRAFT",
        created_by="A",
        created_at=now,
        updated_at=now,
        recommended_quotation_id=None,
        justification=None,
        recommendation=None,
        justification_min_length=None,
        levels_required=None,
        levels_approved=0,
    ).recommendation
    is None,
)


class _Row:  # stands in for an Approval row: the read model must work from attributes
    id, qcs_id, level, approval_cycle = 1, 5, 1, 1
    approver, decision, comments, decided_at = "Bina", "APPROVED", None, now


check("QCSApprovalRead reads from an object's attributes", QCSApprovalRead.model_validate(_Row()).approver == "Bina")

print(f"\n{passed}/{passed + failed} checks passed")
print("ALL PASS" if failed == 0 else "SOME FAILED")