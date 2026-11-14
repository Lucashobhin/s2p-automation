"""One-off: adapt the three older check scripts to the Phase 3c RFQ rules.

RFQs now need requested_quantity, and only ONE open RFQ may exist per PR. The old scripts build
several RFQs per PR directly, so they must use the new_rfq helper (which defaults to CANCELLED).

Run once from backend/, after creating scripts\\_helpers.py with new_rfq:
    python scripts\\patch_old_checks.py
It aborts without changing anything if a file is not what it expects.
"""
from pathlib import Path

EXPECTED = {  # file -> number of "RFQ(pr_id=" constructor calls
    "scripts/check_flow_constraints.py": 10,
    "scripts/check_po_quantity.py": 1,
    "scripts/check_approvals_audit.py": 1,
}
OLD_IMPORT = "from scripts._helpers import cleanup, new_po, new_pr, new_quote, total_rows"
NEW_IMPORT = "from scripts._helpers import cleanup, new_po, new_pr, new_quote, new_rfq, total_rows"

plans = []
for name, count in EXPECTED.items():
    text = Path(name).read_text(encoding="utf-8")
    if "new_rfq(" in text:
        raise SystemExit(f"ABORT: {name} already patched")
    if text.count("RFQ(pr_id=") != count:
        raise SystemExit(f"ABORT: {name}: expected {count} 'RFQ(pr_id=' calls, found {text.count('RFQ(pr_id=')}")
    if text.count(OLD_IMPORT) != 1:
        raise SystemExit(f"ABORT: {name}: expected import line not found exactly once")
    plans.append((name, text.replace("RFQ(pr_id=", "new_rfq(pr_id=").replace(OLD_IMPORT, NEW_IMPORT)))

for name, text in plans:
    Path(name).write_text(text, encoding="utf-8")
    print("patched:", name)