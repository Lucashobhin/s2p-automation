# Architecture Decision Log — D1 to D79

This document records the architectural and business-rule decisions made during development of the S2P procurement automation POC.

Decisions are numbered sequentially. Later decisions may refine or supersede earlier ones.

**Status conventions**
- **Accepted:** Current agreed decision.
- **Superseded:** Historical decision replaced by a later decision.
- **Refined:** Original decision remains relevant with additional constraints.

---

## Slice 2c-ii — RFQ, Quotation, QCS and PO Tables

### D1 — Multiple RFQ rounds per PR
**Status: Accepted (original one-RFQ-per-PR rule superseded).**

Allow multiple RFQ rounds for a PR.

- Unique constraint on `(pr_id, round_number)`.
- RFQ generation has a separate idempotency key.

### D2 — RFQ vendor invitations
**Status: Accepted.**

Use an `rfq_vendors` table to record invited vendors.

A vendor can be associated with an RFQ only once.

### D3 — One quotation per vendor per RFQ
**Status: Superseded by D38.**

The original design allowed one quotation per vendor per RFQ without revisions.

D38 replaced this with quotation revisions and a single ACTIVE revision.

### D4 — Monetary calculation rules
**Status: Accepted.**

- Use Python `Decimal` for monetary calculations.
- The server computes all monetary totals.
- Net price excludes tax.
- Tax is rounded using half-up rounding to two decimal places.
- Total amount includes tax.
- No discounts or freight in the current calculation.

### D5 — QCS table
**Status: Accepted.**

QCS is a separate table, with one QCS per RFQ.

### D6 — PO structure
**Status: Accepted.**

- A PO contains a single line.
- One PO is associated with one quotation.
- ERP reference remains empty until assigned by mock SAP.

### D7 — PO quantity per PR
**Status: Superseded by B-lite.**

Original proposal: one PO per PR.

Revised design: multiple POs per PR, bounded by the PR's `ordered_quantity` counter and requested quantity.

### D8 — One open RFQ per PR
**Status: Accepted and strengthened in Phase 3c.**

Only one open RFQ per PR.

Initially enforced in service code; subsequently enforced using a database partial unique index.

---

## Slice 2c-iii — Approval and Audit Tables

### D9 — Approval table structure
**Status: Accepted.**

One Approval table contains three real foreign keys:

- `pr_id`
- `qcs_id`
- `po_id`

Exactly one entity reference must be populated.

Foreign keys use `ON DELETE RESTRICT`.

### D10 — Approval decisions
**Status: Accepted.**

Supported decisions:

- `APPROVED`
- `REJECTED`
- `RETURNED`

`RETURNED` is PR-only and is included in PR statuses.

### D11 — Approval levels and cycles
**Status: Refined.**

Approval records contain `level` and `approval_cycle`.

For PR approvals:

`approval_cycle = 1 + number of RETURNED decisions`

One decision is allowed per entity, level and cycle. A RETURNED PR starts a new cycle.

Duplicate attempts are recorded in the audit history.

### D12 — Audit entity references
**Status: Accepted.**

Audit events refer to entities through free-text identifiers rather than foreign keys.

### D13 — Audit before/after states
**Status: Accepted.**

Store before and after states as JSON text.

Include an indexed `correlation_id`.

### D14 — Transactional audit
**Status: Accepted.**

Write the audit record in the same database transaction as the associated state change.

The audit helper must not commit independently.

---

## Slice 3a — Create PR

### D15 — API route prefix
**Status: Accepted.**

All application API routes use `/api`.

The `/` and `/health` endpoints remain at the root.

### D16 — Idempotency
**Status: Accepted.**

Use an `Idempotency-Key` header and a hash of the normalized request payload.

The key is protected by a database uniqueness constraint.

- Same key and same body: return the original result.
- Same key and different body: return HTTP 409.

### D17 — PR validation
**Status: Accepted as initial POC rules.**

- Item must exist.
- Quantity must be an integer from 1 to 1,000,000.
- Budget uses Decimal.
- INR only.
- Delivery date must be today or later.
- Names are trimmed.

### D18 — Requester identity
**Status: Accepted.**

The requester is treated as the actor creating the PR.

Identity is unverified in the current POC.

### D19 — PR creation response
**Status: Accepted.**

Successful PR creation returns HTTP 201 and includes `pr_number`.

Initial status is `SUBMITTED`.

Money is serialized as strings, and `PR_CREATED` is audited in the same transaction.

---

## Slice 3b — PR Approval

### D20 — Approval endpoints
**Status: Accepted.**

Provide decision, resubmit and approval-history endpoints.

The server derives the approval level.

### D21 — Required approval levels
**Status: Accepted.**

Approval levels come from `required_approval_levels`.

Default is one level.

### D22 — Separation of duties
**Status: Accepted.**

An approver cannot approve their own PR.

The current implementation uses name comparison rather than authenticated identity.

### D23 — Conditional state transitions
**Status: Accepted.**

Use a state machine with RETURNED, approval cycles and conditional database updates.

### D24 — Comments and resubmission
**Status: Accepted.**

Comments are required for REJECTED and RETURNED decisions.

Resubmission sends the complete PR and applies the same validation rules.

### D25 — PR decision idempotency
**Status: Refined.**

Decision keys are scoped to the PR.

The request hash includes:

- Approver
- Decision
- Comments

Refused attempts are audited.

---

## Slice 3c — RFQ Generation, Publishing and Cancellation

### D26 — RFQ endpoints
**Status: Accepted.**

RFQ API supports:

- Generate
- Publish
- Cancel
- Active RFQ lookup
- Individual RFQ retrieval

n8n is intended to notify vendors in a future phase.

### D27 — RFQ generation
**Status: Accepted.**

Generation requires:

- PR status APPROVED.
- Remaining quantity greater than zero.
- No existing open RFQ for the PR.

Store `requested_quantity`.

Round number is one plus the highest existing round number.

### D28 — RFQ publishing validation
**Status: Accepted.**

Publishing requires:

- RFQ status DRAFT.
- 1–20 distinct active vendors.
- Vendors must belong to the item's category.
- Deadline must be between tomorrow and the PR delivery date.

OEM is not enforced. NON_OEM is represented by a flag.

### D29 — Vendor selection
**Status: Accepted.**

The buyer supplies `vendor_ids`.

### D30 — PR status independence
**Status: Accepted.**

RFQ activity does not change PR status.

### D31 — Original RFQ cancellation
**Status: Superseded and extended by D43.**

Originally, DRAFT or PUBLISHED RFQs could be cancelled only without quotations, with a reason required.

D43 extended cancellation to CLOSED RFQs under additional restrictions.

### D32 — RFQ audit and idempotency
**Status: Accepted.**

Audit events:

- `RFQ_GENERATED`
- `RFQ_PUBLISHED`
- `RFQ_CANCELLED`

Audit is written in the same transaction as the state change.

Only generation uses an idempotency key.

### D33 — RFQ actor identity
**Status: Accepted.**

RFQ actor identity is unverified in the current POC.

---

## Slice 3d — Vendor Quotations

### D34 — Quotation endpoints
**Status: Accepted.**

Provide quotation submission, listing and retrieval endpoints.

### D35 — Vendor identity
**Status: Accepted (Option A).**

Vendor identity is the claimed `vendor_id`.

Authentication tokens are deferred to Phase 8.

### D36 — Server-side quotation calculations
**Status: Accepted.**

Vendor sends:

- Price
- Tax rate
- Quantity
- Lead time

The server calculates totals. Client-supplied totals are rejected.

### D37 — Quotation submission validation
**Status: Accepted.**

Submission requires:

- RFQ PUBLISHED.
- Current date on or before deadline.
- Vendor invited; otherwise HTTP 403.
- Quantity no greater than requested quantity.
- Currency matching the PR.
- Lead time from 0 to 365 days, configurable.

### D38 — Quotation revisions
**Status: Accepted; supersedes D3.**

Allow quotation revisions until the deadline.

Statuses:

- ACTIVE
- SUPERSEDED

Only one ACTIVE quotation per RFQ/vendor is allowed.

Previous revisions remain in history.

### D39 — Quotation idempotency
**Status: Refined.**

Idempotency is scoped to:

`(rfq_id, vendor_id, idempotency_key)`

Identical content is a no-op returning HTTP 200.

A late retry of an older revision returns that original revision, even if it has since been superseded.

### D40 — RFQ status after deadline
**Status: Accepted.**

Quotations are accepted only while RFQ is PUBLISHED.

Passing the deadline does not automatically close the RFQ. It remains PUBLISHED until explicitly closed.

### D41 — Quotation audit and sealed bids
**Status: Accepted.**

Audit events:

- `QUOTATION_SUBMITTED`
- `QUOTATION_REVISED`

Sealed-bid switch defaults to OFF.

When enabled, quotation visibility is blocked for everyone until the deadline has passed.

---

## Slice 3e — RFQ Closing, Cancellation and Comparison

### D42 — RFQ closing
**Status: Accepted.**

An RFQ can transition from PUBLISHED to CLOSED only after its deadline and when at least one ACTIVE quotation exists.

Early closing is not allowed.

### D43 — Extended RFQ cancellation
**Status: Accepted; extends D31.**

A CLOSED RFQ can be cancelled even when quotations exist.

Cancellation is blocked by:

- QCS status PENDING_APPROVAL or APPROVED.
- An active PO.

### D44 — Comparison behavior
**Status: Accepted.**

Comparison presents facts only.

No scoring, ranking or winner selection.

Only ACTIVE revisions are included.

The lowest marker uses tax-inclusive unit price.

### D45 — Partial quotation comparison
**Status: Refined.**

Show coverage against requested quantity.

Compare total amounts only when quoted quantities match.

Tax-inclusive unit price is held to four decimal places.

### D46 — Close/cancel audit
**Status: Accepted.**

Close and cancel are audited in the same transaction as their state changes.

Comparison is read-only.

### D47 — Sealed-bid comparison
**Status: Accepted.**

Comparison follows the same sealed-bid switch as the quotation list.

---

## Slice 3f — QCS Creation, Editing and Submission

### D48 — QCS creation and idempotency
**Status: Accepted.**

Create QCS only for CLOSED RFQs.

One QCS per RFQ.

Creation idempotency is scoped to `(rfq_id, idempotency_key)` and uses a normalized request-body hash.

Replay is checked FIRST, before checking current RFQ state.

A new request requires RFQ CLOSED.

### D49 — Recommended quotation
**Status: Accepted.**

Recommendation is optional in DRAFT and required at submission.

It must be an ACTIVE quotation belonging to the same RFQ.

### D50 — Justification validation
**Status: Accepted.**

Justification is required at submission and has a maximum of 1,000 characters.

Minimum length:

- 20 characters normally.
- 80 characters if the recommendation is not the lowest tax-inclusive unit-price quotation or is a partial quote.

Ties count as lowest.

This is a validation speed bump, not proof of a good decision.

### D51 — QCS editing
**Status: Accepted.**

PATCH is allowed only when QCS is DRAFT and RFQ is CLOSED.

### D52 — Submit and withdraw idempotency
**Status: Accepted.**

Submit and withdraw do not use idempotency keys.

State validation and structured errors govern repeated requests.

### D53 — QCS approval levels
**Status: Accepted; placeholder.**

`rules.required_qcs_approval_levels(quotation)` returns one level by default.

Future thresholds must use quotation `total_amount`.

### D54 — QCS decisions
**Status: Accepted.**

Decisions are APPROVED and REJECTED only.

RETURNED is PR-only.

REJECTED requires comments.

Decision key is scoped to `(qcs_id, idempotency_key)`.

### D55 — QCS metadata columns
**Status: Accepted.**

Add nullable columns:

- `created_by`
- `idempotency_key`
- `request_hash`

Nullable columns avoid a risky SQLite rebuild.

Service and schema require a nonblank creator for new records.

### D56 — QCS withdrawal status
**Status: Refined by D75–D79.**

An approved QCS can transition to WITHDRAWN.

Status is a plain string without a database CHECK constraint or migration.

### D57 — QCS API and error responses
**Status: Accepted.**

QCS operations include create, retrieve, edit, submit, decide and approval-history operations.

Errors use structured responses with `code`, `message` and relevant state details.

### D58 — QCS scope limitations
**Status: Accepted.**

Phase 3f excludes:

- Scoring
- AI fields
- RFQ AWARDED status
- PO creation

### D59 — QCS read response
**Status: Accepted.**

Read responses include recommendation details, required approval levels and approved-level count.

### D60 — QCS actor rules
**Status: Accepted.**

Edit and submit are restricted to the creator using normalized name comparison.

Withdrawal is available to any named actor and requires a reason.

### D61 — QCS structured errors
**Status: Accepted.**

Return explicit error codes and relevant state information rather than generic conflicts.

### D62 — QCS audit events
**Status: Accepted.**

Audit events include:

- `QCS_CREATED`
- `QCS_UPDATED`
- `QCS_SUBMITTED`
- `QCS_APPROVAL_LEVEL_APPROVED`
- `QCS_APPROVED`
- `QCS_REJECTED`
- `QCS_WITHDRAWN`
- `DUPLICATE_DECISION_ATTEMPT`

State-changing audit follows D14. Refused decision attempts follow D72.

---

## Block B — QCS Draft Editing and Submission

### D63 — PATCH and submit check order
**Status: Accepted.**

Check order:

1. 404 QCS_NOT_FOUND.
2. 403 NOT_THE_CREATOR.
3. 409 QCS_NOT_DRAFT, including `qcs_status`.
4. 409 RFQ_NOT_CLOSED.
5. 422 content validation.

NULL creator means nobody passes the creator check.

### D64 — QCS PATCH semantics
**Status: Accepted.**

- Only supplied fields are changed, using `model_fields_set`.
- Explicit null clears a nullable field.
- No fields supplied: 422 NOTHING_TO_UPDATE.
- Identical content: 200 with no write, audit or timestamp update.
- Changed content: one conditional UPDATE requiring QCS DRAFT and RFQ CLOSED.

A lost race returns QCS_CHANGED with actual state.

### D65 — QCS submission
**Status: Accepted.**

Submission validates:

- Recommendation exists.
- Recommendation remains ACTIVE.
- Justification satisfies D50.
- RFQ remains CLOSED.

One conditional UPDATE rechecks:

- QCS is DRAFT.
- RFQ is CLOSED.
- Recommendation and justification match validated values.
- Recommendation is still ACTIVE.

Audit is written in the same transaction.

### D66 — Draft operation races
**Status: Accepted.**

After a failed conditional UPDATE, re-read actual database state and return the corresponding conflict rather than a generic error.

---

## Block C — QCS Approval

### D67 — Decision check order and replay
**Status: Accepted.**

1. Check matching idempotency replay.
2. 404 QCS_NOT_FOUND.
3. 403 separation-of-duties violation.
4. 409 QCS_NOT_PENDING.
5. 409 RFQ_NOT_CLOSED.

On refusal paths, check whether the request is the caller's own matching replay first.

### D68 — QCS approval separation of duties
**Status: Accepted.**

Approver cannot be:

- QCS creator.
- PR requester.
- Someone who already decided a level of this QCS.

Names are normalized.

A NULL creator skips only the creator comparison for legacy records.

### D69 — Approval levels and final decisions
**Status: Accepted.**

Next level:

`approved decisions so far + 1`

QCS approval cycle is always 1.

Required levels come from `required_qcs_approval_levels(recommended quotation)`.

- Intermediate APPROVED leaves QCS PENDING_APPROVAL.
- Final APPROVED changes QCS to APPROVED.
- REJECTED is final at any level.

### D70 — Decision transaction ordering
**Status: Accepted.**

One transaction:

1. Conditional QCS UPDATE first, requiring PENDING_APPROVAL and RFQ CLOSED.
2. Insert approval record.
3. Write audit.
4. Commit.

Intermediate approval uses `updated_at = updated_at`, preserving the timestamp.

Its audit has no before-state and no status key because QCS status has not changed.

### D71 — Lost decision UPDATE
**Status: Accepted.**

On a failed conditional UPDATE:

- Roll back.
- Re-read actual state.
- Return QCS_NOT_PENDING, RFQ_NOT_CLOSED or QCS_CHANGED as appropriate.

### D72 — Refused decision audit
**Status: Accepted.**

Attempts against APPROVED, REJECTED or WITHDRAWN QCS, and lost decision races, write a refused-attempt audit in a separate transaction.

If that audit fails, preserve the original 409 and log a warning.

### D73 — Decision idempotency replay
**Status: Accepted.**

Replay returns the original approval plus current `qcs_status` only for the same normalized approver and same content.

Different approver or content with the same key returns neutral 409 IDEMPOTENCY_KEY_REUSED.

The error must not reveal another approver's identity or decision.

### D74 — Approval aftermath
**Status: Accepted.**

Approval does not award the RFQ, create a PO or change RFQ status.

The RFQ stays CLOSED.

REJECTED is final; buyer cancels and re-tenders.

---

## Block D — QCS Withdrawal

### D75 — Withdrawal endpoint and validation order
**Status: Accepted.**

Endpoint:

`POST /api/qcs/{id}/withdraw`

Check order:

1. 404 QCS_NOT_FOUND.
2. 422 NAME_REQUIRED for blank actor or invalid reason.
3. 409 QCS_NOT_APPROVED, including `qcs_status`.
4. 409 PO_EXISTS, including purchase-order count.

Reason is trimmed and must contain 1–1,000 characters.

No separation-of-duties 403.

No idempotency key. Repeated withdrawal returns QCS_NOT_APPROVED with WITHDRAWN status.

### D76 — Live PO scope
**Status: Accepted.**

Any live PO on any quotation belonging to the QCS's RFQ blocks withdrawal.

Blocking statuses are `ACTIVE_PO_STATUSES`:

- PENDING_APPROVAL
- APPROVED
- ERP_PUSHED

CANCELLED and REJECTED do not block.

This matches the rule used by RFQ cancellation.

### D77 — Atomic withdrawal
**Status: Accepted.**

One transaction and one conditional UPDATE requiring:

- Matching QCS ID.
- Status APPROVED.
- No live PO on any quotation of the RFQ.

The NOT EXISTS PO condition must be inside the conditional database UPDATE.

Set status WITHDRAWN and update `updated_at`.

Write the audit in the same transaction.

Approval rows remain unchanged as history.

### D78 — Withdrawal race handling
**Status: Accepted.**

If the conditional UPDATE affects no row:

1. Roll back.
2. Re-read actual state.

Return:

- QCS_NOT_APPROVED with `qcs_status` if QCS is no longer APPROVED.
- PO_EXISTS with count if a live PO exists.
- QCS_CHANGED otherwise.

### D79 — Withdrawal audit and aftermath
**Status: Accepted.**

Audit event: `QCS_WITHDRAWN`.

Before:

`{"status": "APPROVED"}`

After:

`{"status": "WITHDRAWN", "reason": "..."}`

No refused-withdrawal audit event.

WITHDRAWN is terminal. Edit, submit, decide and repeated withdrawal are refused.

RFQ remains CLOSED. Buyer can cancel and re-tender.

The unique constraint `uq_qcs_rfq` prevents creating a second QCS for the same RFQ.

---

## Future Phase 3g Requirement

PO creation must independently re-check that the QCS is APPROVED inside its own conditional database write.

D77 protects the withdrawal side of the race. Phase 3g must implement the PO-creation side.

PO creation is not part of Phase 3f.

