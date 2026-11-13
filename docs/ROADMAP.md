# Source-to-Pay (S2P) Automation — Full Roadmap

### Phase 1: Skeleton
- 1a. venv, FastAPI, uvicorn
- 1b. / and /health routes
- 1c. Git init, .gitignore, pinned requirements.txt

### Phase 2: Data layer
- 2a. Config, DB session, Vendor model, Alembic wired, migration with downgrade tested
- 2b-i. Vendor schemas + GET/POST /vendors
- 2b-ii. Seed 50 sample vendors
- 2c. Remaining tables: PR + items, Approval, RFQ + RFQ-vendors, Quotation + items, QCS, Purchase Order, Audit Log
- 2d. Status enums, idempotency keys, audit conventions

### Phase 3: Core flow, no AI
- 3a. Create PR with validation
- 3b. PR approval (thresholds in config, placeholders)
- 3c. RFQ auto-created from approved PR
- 3d. RFQ publish to vendors
- 3e. Vendor quotation submit (API)
- 3f. Quote comparison, deterministic
- 3g. QCS create + approval
- 3h. PO create from approved QCS
- 3i. State-machine guards + audit entry on every transition

### Phase 4: Mock SAP (plays the CPI/ABAP role from Metrix)
- 4a. Mock SAP endpoints: PR, RFQ, quotation, PO
- 4b. Adapter interface, so real SAP can swap in later
- 4c. Push sync with retry, failure handling, no duplicate POs
- 4d. SAP-style document numbers (optional)

### Phase 5: Vendor scoring, rule-based
- 5a. Scoring weights as config, marked "placeholder, needs procurement sign-off"
- 5b. Compliance and OEM rules, non-OEM = HIGH RISK
- 5c. Ranking: compliance > price > lead time > performance
- 5d. Explainable output (why this rank)
- 5e. Vendor master adapter: DB now, Google Sheets later

### Phase 6: AI layer (bounded, never approves or pays)
- 6a. integrations/llm.py wrapper (Gemini default, swappable)
- 6b. Structured output via Pydantic
- 6c. Vendor Discovery agent
- 6d. RFQ Comparison agent
- 6e. Quote extraction from PDF (PyMuPDF/pdfplumber, Tesseract for scans)
- 6f. Confidence score (labelled)
- 6g. Guardrails + labelled eval set before claiming any accuracy number

### Phase 7: n8n orchestration (port 5678)
- 7a. Install and run n8n
- 7b. Webhook trigger into FastAPI
- 7c. The 11-step pipeline from the DOCX
- 7d. Email send and inbound (SMTP)
- 7e. Retries and error paths
- 7f. API auth between n8n and FastAPI

### Phase 8: Portals (Vite + React + Tailwind)
- 8a. Project setup (buyer 5173, vendor 5174)
- 8b. Buyer: PR list/create, approvals, RFQ publish
- 8c. Quote comparison + QCS view
- 8d. PO view + audit timeline
- 8e. Vendor portal: RFQ inbox, submit quote, status
- 8f. Dashboard modules (the 9 from the DOCX)
- 8g. Login and roles

### Phase 9: Hardening and demo
- 9a. pytest unit tests
- 9b. Full-flow integration test
- 9c. Failure cases: duplicate request, rejected PR, bad PDF, no vendors, non-OEM vendor
- 9d. Demo scenarios A, B, C
- 9e. TRACEABILITY.md (requirement -> doc section -> test)
- 9f. README + Docker compose
- 9g. Demo script

### Phase 10: Optional extension
GRN, invoice, 3-way match, duplicate-invoice check, payment. The Metrix PDF doesn't cover these. The Matrix Comsec DOCX does.
