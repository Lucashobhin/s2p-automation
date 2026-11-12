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
| A9 | Approval hierarchy & multi-level thresholds undefined | config / core | PO/PR approval chain logic is incomplete | High | QUESTION FOR SENIOR |
| A10 | PR item reference must point to an active record in items table | models/purchase_requisition.py | Orphan PRs or PRs for non-existent inventory parts | High | Decided, tested |
| A11 | PR quantity must be strictly positive integer (> 0) | models/purchase_requisition.py | 0 or negative quantities corrupt procurement requirements | High | Decided, tested |
| A12 | Estimated budget must be non-negative (>= 0) | models/purchase_requisition.py | Negative financial values break pricing and budget limits | High | Decided, tested |
| A13 | PR default currency is INR, 3-character ISO code | models/purchase_requisition.py | Multi-currency conversions not supported in Phase 2 | Medium | Decided |
| A14 | PR idempotency key is unique (64-char string) | models/purchase_requisition.py | Network retries from n8n or UI create duplicate PRs | High | Decided, tested |
| A15 | PR statuses defined as enum: DRAFT, SUBMITTED, APPROVED, REJECTED, RFQ_IN_PROGRESS, ORDERED, CANCELLED | core/enums.py | State machine bypass if arbitrary strings are permitted | Medium | Decided |
| A16 | Vendor catalog IDs follow pattern V-XXXXX and Item codes follow standard SKU pattern | models/vendor.py, models/item.py | Bad ERP cross-referencing during data migration | Low | Decided, tested |
| A17 | PR delivery date must be on or after creation date | models/purchase_requisition.py | Past delivery dates on new purchase requisitions | Low | Decided, tested |
| A18 | D1: several RFQ rounds per PR, unique (pr_id, round_number); idempotency key is a separate concern | models/rfq.py | Unique pr_id would block re-tendering; a missing key lets retries create duplicate RFQs | Medium | Decided, tested |
| A19 | D2: only invited vendors may quote (DB composite foreign key) | models/quotation.py | If RFQs are open to all vendors, legitimate quotes get rejected | Medium | Placeholder, tested |
| A20 | D3: one quotation per vendor per RFQ, no revisions. Known limitation: Metrix says vendors resend quotes, so real life has revisions | models/quotation.py | A vendor cannot correct a quote; needs a revision column plus a migration | Medium | Known limitation |
| A21 | D4: unit_price is net; tax_amount = net x rate, rounded half-up to 2 dp; total includes tax; no discounts or freight; server computes, client totals are ignored | services/pricing.py | Wrong totals flow into QCS ranking and PO amounts | High | Placeholder, needs finance sign-off |
| A22 | quoted_quantity may differ from the PR quantity. Partial quotes are supported through later RFQ rounds (B-lite). QCS must flag a partial quote and not rank it as equal | models/quotation.py | A partial quote looks cheapest and wins unfairly | Medium | Pending (Phase 5) |
| A23 | D5: one QCS per RFQ; its recommended quotation must belong to the same RFQ (DB enforced) | models/qcs.py | If a real QCS spans several RFQs this table shape is wrong | Low | Decided, tested |
| A24 | D6: one PO per quotation, single line; erp_reference unique once set | models/purchase_order.py | Split or partial POs from one quote are impossible | Low | Decided, tested |
| A25 | B-lite (replaces D7): several POs per PR, sequentially. PR.ordered_quantity counts units in ACTIVE POs (PENDING_APPROVAL, APPROVED, ERP_PUSHED), CHECK ordered <= quantity, changed only via services/po_quantity.py | models/purchase_requisition.py | Code that updates the counter directly bypasses the guard; over-ordering or a blocked PR | High | Decided, tested incl. concurrency |
| A26 | D8: only one open RFQ per PR, enforced in service code (Phase 3), not the DB | Phase 3 | Two live RFQs for one need; vendors get conflicting requests | Medium | Pending (Phase 3) |
| A27 | NOT DB-enforced, so service code and tests must check: PO.pr_id equals the PR behind the quotation's RFQ; PO.quantity <= quotation.quoted_quantity; quotation currency equals PR currency | Phase 3 | PO linked to the wrong PR, ordering more than the vendor quoted, or mixed currencies | High | Pending (Phase 3) |
| A28 | RFQ, Quotation, QCS and PO status values invented; not checked by the DB, so changing them needs no migration | core/enums.py | Wrong transitions; SAP status mapping breaks | Medium | Placeholder |
| A29 | SPLIT-AWARD POLICY UNDEFINED: one QCS decision awarding portions to several vendors (needs allocation lines and a rule: by rank, price, capacity?) is out of scope | Phase 5 | Cannot award 60 to vendor X and 40 to vendor Y in one decision | High | QUESTION FOR SENIOR |
| A30 | A REJECTED or CANCELLED PO releases its units. Cancelling an ERP_PUSHED PO is blocked until an ERP reversal flow exists | services/po_quantity.py | Rejected POs would block their PR forever; or a cancelled PO stays live in SAP | Medium | QUESTION FOR SENIOR |
| A31 | The counter is a stored copy of derived data; find_quantity_mismatches detects drift. It must run in Phase 3 and Phase 9 tests | services/po_quantity.py | Silent drift over time (counter differs from real POs) | Medium | Decided |
| A32 | RFQ issue_date and response_deadline stay empty until publish, because the RFQ is auto-created from the PR before anyone knows the deadline (Metrix steps 3 and 4). The CHECK deadline >= issue_date applies only when both are set | models/rfq.py | If a real RFQ must carry a deadline from creation, Phase 3 needs default dates | Medium | Placeholder |
| A33 | All guards were tested on SQLite only (3.50.4). PostgreSQL behaviour of composite foreign keys, the conditional UPDATE under concurrency, and Numeric is untested | all models | A guard that works on SQLite behaves differently on PostgreSQL after a later switch | Medium | Known limitation |
| A34 | Approval: exactly one of pr_id/qcs_id/po_id (real foreign keys, RESTRICT). Option A: ONE decision per entity, level and cycle, final within a cycle; a changed decision needs a RETURN and resubmission. Duplicate attempts must be logged as audit events (Phase 3) | models/approval.py | A manager who misclicks cannot correct it until the PR is returned; if corrections are a must, a superseded mechanism is needed | Medium | Decided, tested |
| A35 | approval_cycle = 1 + number of RETURNED decisions for that entity, derived by the service and not stored. Level N needs level N-1 approved in the same cycle (service rule) | Phase 3 | A wrong cycle number causes false duplicate rejections or skipped levels | Medium | Pending (Phase 3) |
| A36 | RETURNED applies to PRs only (docs). PR transitions SUBMITTED to APPROVED/REJECTED/RETURNED and RETURNED to SUBMITTED are defined in Phase 3 | core/enums.py | Wrong transitions strand a PR | Medium | Pending (Phase 3) |
| A37 | Audit events reference entities by free text (type + id), with no foreign key; before/after are JSON text; correlation_id is indexed | models/audit_event.py | A mistyped entity id is accepted silently; JSON cannot be queried efficiently on SQLite | Low | Decided, tested |
| A38 | Audit rows are written with record_audit in the SAME transaction as the state change. The database cannot enforce this; anything that writes directly (scripts, future n8n nodes) bypasses it | services/audit.py | A state change exists with no audit row; the "full audit trail" claim fails | High | Pending (Phase 3 test: forced audit failure rolls back) |
| A39 | Approvals and audit events are append-only by convention only. Tamper-proofing (database triggers) is Phase 9 hardening | models | Rows could be edited or deleted undetected | Medium | Deferred |
