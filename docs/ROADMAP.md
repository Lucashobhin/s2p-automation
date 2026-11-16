# Source-to-Pay (S2P) Automation — Implementation Roadmap

## Phase 1: Project Skeleton & Core Architecture (Completed)
- FastAPI backend structure, configuration, environment settings, and `/api` routing prefix.
- Decimal-precision monetary helpers, header sanitization (`Idempotency-Key`, `X-Correlation-ID`), and structured error contract conventions.

## Phase 2: Database Schema, Migrations & Seed Data (Completed)
- SQLAlchemy 2.0 models and Alembic migrations for `items`, `vendors`, `purchase_requisitions`, `rfqs`, `rfq_vendors`, `quotations`, `qcs`, `purchase_orders`, `approvals`, and `audit_events`.
- Database-level check constraints, foreign keys, and master data seeders (50 vendors, 12 items).

## Phase 3: Core Deterministic S2P State Machine

### Phase 3a: Purchase Requisition (PR) Intake (Completed)
- `POST /api/requisitions` and `GET /api/requisitions` with payload validation, request-hash idempotency, and atomic `PR_CREATED` audit logging.

### Phase 3b: Multi-Level PR Approval & Resubmission (Completed)
- Approval decision flow (`APPROVED`, `RETURNED`, `REJECTED`) across configurable approval levels and resubmission cycles.
- Self-approval prevention, cycle/level-scoped idempotency, and transactional rollback on audit failure.

### Phase 3c: RFQ Generation, Publishing & Cancellation (Completed)
- `POST /api/rfq/generate`, `POST /api/rfq/{id}/publish`, and `POST /api/rfq/{id}/cancel`.
- Partial unique index enforcing at most one open RFQ (`DRAFT`, `PUBLISHED`, `CLOSED`) per PR, vendor eligibility validation, and PR remaining-quantity checks.

### Phase 3d: Vendor Quotations & Revisions (Completed)
- `POST /api/rfq/{id}/quotations`, `GET /api/rfq/{id}/quotations`, and `GET /api/quotations/{id}`.
- Server-side `Decimal` calculation of net, tax, and total amounts; revision tracking (`ACTIVE` vs. `SUPERSEDED` history); vendor-scoped idempotency; and configurable sealed-bid visibility (`SEAL_QUOTATIONS_UNTIL_DEADLINE`).

### Phase 3e: RFQ Close, Quote Comparison & Post-Close Cancellation (In Progress)
- `POST /api/rfq/{id}/close` (`PUBLISHED` -> `CLOSED` after response deadline with at least one `ACTIVE` quotation).
- `GET /api/rfq/{id}/comparison` read-only factual comparison table (coverage %, partial-quote flags, 4-decimal tax-inclusive unit price, lowest price/shortest lead-time/full-coverage markers, no automated scoring).
- Allow cancelling a `CLOSED` RFQ (with reason) when quotes are unacceptable, provided no QCS is `PENDING_APPROVAL`/`APPROVED` and no active PO exists.

### Phase 3f: Quotation Comparison Sheet (QCS) Lifecycle
- Create, update, submit for approval, and approve/reject a QCS (`DRAFT` -> `PENDING_APPROVAL` -> `APPROVED` / `REJECTED`).
- Enforces one QCS per RFQ and records human approval decisions in the polymorphic `approvals` table.

### Phase 3g: Purchase Order (PO) Creation & Approval
- Generate a Purchase Order from an `APPROVED` QCS (`PENDING_APPROVAL` -> `APPROVED` / `REJECTED`).
- Reserves ordered quantity against the PR (`ordered_quantity` via `ACTIVE_PO_STATUSES`) and transitions the RFQ to `AWARDED`.

### Phase 3h: State-Machine Guards & PR Lifecycle Cleanup
- PR cancellation and delivery-date extension endpoints so expired PRs do not stall permanently.
- Unify structured error responses (`{code, message, ...}`) across all Phase 3a/3b endpoints and run end-to-end state-machine regression checks.

## Phase 4: Mock SAP Adapter & ERP Integration
- Swappable `SAPAdapter` interface (`app/integrations/sap.py`) with `MockSAPAdapter` implementation.
- Inbound material, vendor, and PR syncs plus outbound PO push (`APPROVED` -> `ERP_PUSHED` with ERP document reference) and simulated downtime/retry testing.

## Phase 5: Rule-Based Scoring & Deterministic Ranking
- Configurable evaluation criteria and weights in `app/core/rules.py` (compliance, tax-inclusive price, lead time, vendor performance) to rank quotations deterministically prior to AI justification.

## Phase 6: Bounded AI Layer (3 Specialized Agents)
- **Vendor Discovery Agent:** Suggests matching vendors for a PR based on item category and historical performance.
- **Quotation Extraction Agent:** Parses vendor PDF/email quotes into validated structured payloads for `POST /api/rfq/{id}/quotations`.
- **Evaluation & Recommendation Agent:** Analyzes comparison and rule-scoring outputs to populate explainable QCS recommendations while keeping final approval human-in-the-loop.

## Phase 7: n8n Workflow Orchestration & SMTP Notifications
- Event-driven and scheduled n8n workflows automating RFQ dispatch, post-deadline RFQ closing, reminder notifications, and idempotent retry handling.

## Phase 8: Buyer & Vendor React Portals
- Vite + React + Tailwind frontend with separate Buyer Portal (PRs, approvals, RFQs, comparison tables, QCS/PO approvals, audit logs) and Vendor Portal (invited RFQs and quote submission/revisions).

## Phase 9: Hardening, Test Suite & Docker Compose Packaging
- Full `pytest` regression suite, traceability documentation (`TRACEABILITY.md`), end-to-end demo scripts, and multi-container `docker-compose.yml` setup.

## Phase 10: Post-PO Operations (Optional Scope)
- Goods Receipt Note (GRN), vendor invoice capture, and 3-way matching (PO vs. GRN vs. Invoice).
