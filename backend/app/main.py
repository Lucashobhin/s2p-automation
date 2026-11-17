from fastapi import FastAPI

from app.api import comparison, items, quotations, requisitions, rfq, vendors

app = FastAPI(
    title="S2P POC - Mock SAP MM + Procurement API",
    version="0.1.0",
    description="POC. Identities (a PR's requester, an RFQ's actor, a quotation's vendor) are user-supplied and NOT "
    "verified: there is no authentication yet.",
)

app.include_router(vendors.router, prefix="/api")
app.include_router(items.router, prefix="/api")
app.include_router(requisitions.router, prefix="/api")
app.include_router(rfq.router, prefix="/api")
app.include_router(quotations.router, prefix="/api")
app.include_router(comparison.router, prefix="/api")


@app.get("/")
def root():
    return {
        "message": "S2P POC API is running",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health():
    return {"status": "ok", "mode": "POC-MOCK"}