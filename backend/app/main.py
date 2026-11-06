from fastapi import FastAPI

from app.api import vendors

app = FastAPI(
    title="S2P POC - Mock SAP MM + Procurement API",
    version="0.1.0",
)

app.include_router(vendors.router)


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
