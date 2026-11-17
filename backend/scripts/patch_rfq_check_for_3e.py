"""One-off: update scripts/check_rfq_api.py for the Phase 3e cancel rule.

3c asserted "a CLOSED RFQ cannot be cancelled yet". Phase 3e (D43) deliberately reverses that: a CLOSED RFQ
CAN be cancelled, even with quotations. Run once from backend/:
    python scripts\\patch_rfq_check_for_3e.py
It aborts without changing anything if the file is not what it expects, and refuses a second run.
"""
from pathlib import Path

PATH = Path("scripts/check_rfq_api.py")
OLD = '''        set_rfq(d11, status="CLOSED")
        r = cancel(d11)
        check("a CLOSED RFQ cannot be cancelled yet -> 409 RFQ_NOT_CANCELLABLE", r.status_code == 409 and code(r) == "RFQ_NOT_CANCELLABLE" and detail_of(r).get("rfq_status") == "CLOSED")
        r = gen(p11)
        check("a CLOSED RFQ still BLOCKS a new round (it is open) -> 409", r.status_code == 409 and code(r) == "OPEN_RFQ_EXISTS" and detail_of(r).get("rfq_status") == "CLOSED")
'''
NEW = '''        set_rfq(d11, status="CLOSED")
        r = gen(p11)
        check("a CLOSED RFQ still BLOCKS a new round (it is open) -> 409", r.status_code == 409 and code(r) == "OPEN_RFQ_EXISTS" and detail_of(r).get("rfq_status") == "CLOSED")
        r = cancel(d11)
        check("3e (D43): a CLOSED RFQ WITH quotations can now be cancelled -> 200", r.status_code == 200 and r.json().get("status") == "CANCELLED", f"status={r.status_code}")
        check("...and the PR can then be re-tendered (a new round is allowed)", gen(p11).status_code == 201)
'''
text = PATH.read_text(encoding="utf-8")
if "3e (D43)" in text:
    raise SystemExit("ABORT: already patched")
if text.count(OLD) != 1:
    raise SystemExit("ABORT: the expected 3c block was not found exactly once; paste check_rfq_api.py to me.")
PATH.write_text(text.replace(OLD, NEW), encoding="utf-8")
print("patched:", PATH)