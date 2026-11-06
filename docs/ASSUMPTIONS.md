# Assumptions Register

Rule: every guess goes here. Interns rank them. Only **High** severity items
(wrong guess breaks correctness, money or data safety) go to seniors.

| ID | Assumption | Where | If wrong, what breaks | Severity | Status |
|----|-----------|-------|-----------------------|----------|--------|
| A1 | Scales: performance 0-100, quality 1-5, on-time 0-100 | app/core/scales.py | Scoring mixes units, so ranking is silently wrong; API rejects valid data | High | Placeholder |
| A2 | Synthetic vendors; fake GST numbers contain "SYNTH" | scripts/seed_vendors.py | Real GST checksum validation would reject them (harmless in POC) | Low | Accepted |
| A3 | 4 categories taken from DOCX sample sheet | scripts/seed_vendors.py | Discovery by category finds nothing for real category names | Medium | Placeholder |
| A4 | Vendor master lives in DB, Sheets adapter comes later | models/vendor.py | Sheets sync needed later; logic unaffected if behind an interface | Medium | Decided |
| A5 | SQLite now, PostgreSQL later via DATABASE_URL | core/config.py | Type and migration differences surface late | Low | Decided |
| A6 | LLM = Gemini cloud, synthetic data only | Phase 6 | Confidential data leaves the company if real docs are used | High | Pending |
| A7 | Non-OEM = HIGH RISK flag, not a hard block | Phase 5 | Wrongly allows or blocks a PO to a non-OEM vendor | High | Pending |
| A8 | Scoring weights (docs give order only) | Phase 5 | Ranking disagrees with human buyers | High | Pending |
| A9 | PR approval levels and thresholds | Phase 3 | PR auto-approved or routed to the wrong approver | High | Pending |
| A10 | Definition of "confidence" | Phase 6 | Misleading number shown to buyers | Medium | Pending |
| A11 | One item per PR (brief's PR has a single item_id and quantity) | models/purchase_requisition.py | Multi-item PRs impossible; needs a PR-lines table plus a migration | Medium | Placeholder |
| A12 | Status values (SUBMITTED, APPROVED, REJECTED, RFQ_CREATED, CANCELLED) invented; docs define none | core/enums.py | Wrong transitions; SAP sync status mapping breaks | Medium | Placeholder |
| A13 | Integer PK, display number PR-00001; not SAP-style numbering | models/purchase_requisition.py | Numbers differ from real SAP PR numbers; cosmetic | Low | Decided |
| A14 | requester is free text; no user table until login (Phase 8) | models/purchase_requisition.py | Cannot enforce who may approve whom | Medium | Placeholder |
| A15 | quantity is a whole number | models/purchase_requisition.py | Fractional units (metres, kg) rejected | Low | Placeholder |
| A16 | Money = Numeric(14,2), default currency INR; all timestamps stored as UTC (SQLite drops tz) | models | Wrong totals if floats creep in; wrong times if local time is mixed in | Medium | Decided |
| A17 | 12 synthetic items in the same 4 categories as vendors | scripts/seed_items.py | Real item master has other categories; discovery finds no vendors | Low | Placeholder |
