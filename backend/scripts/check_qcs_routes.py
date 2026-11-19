"""Check that the QCS router is wired and the OpenAPI schema builds. No database access.

Run from backend/:  python -m scripts.check_qcs_routes
"""
from app.main import app

passed = failed = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label:<78} {extra}")


try:
    spec = app.openapi()
    check("the OpenAPI schema builds (all models valid)", True)
except Exception as exc:   # a broken response model shows up here
    spec = {"paths": {}, "components": {"schemas": {}}}
    check("the OpenAPI schema builds (all models valid)", False, f"{type(exc).__name__}: {exc}")

paths = spec["paths"]
post_create = paths.get("/api/rfq/{rfq_id}/qcs", {}).get("post", {})

print("--- QCS routes (block A)")
check("POST /api/rfq/{rfq_id}/qcs", "post" in paths.get("/api/rfq/{rfq_id}/qcs", {}))
check("GET /api/rfq/{rfq_id}/qcs", "get" in paths.get("/api/rfq/{rfq_id}/qcs", {}))
check("GET /api/qcs/{qcs_id}", "get" in paths.get("/api/qcs/{qcs_id}", {}))
check("GET /api/qcs/{qcs_id}/approvals", "get" in paths.get("/api/qcs/{qcs_id}/approvals", {}))
check("PATCH /api/qcs/{qcs_id}", "patch" in paths.get("/api/qcs/{qcs_id}", {}))
check("POST /api/qcs/{qcs_id}/submit", "post" in paths.get("/api/qcs/{qcs_id}/submit", {}))
check("POST /api/qcs/{qcs_id}/decision", "post" in paths.get("/api/qcs/{qcs_id}/decision", {}))
check("create documents 201 (new) and 200 (replay)",
      {"201", "200"} <= set(post_create.get("responses", {})))
header_names = {p["name"] for p in post_create.get("parameters", []) if p.get("in") == "header"}
check("create takes Idempotency-Key and X-Correlation-ID headers",
      {"Idempotency-Key", "X-Correlation-ID"} <= header_names, str(sorted(header_names)))
read_props = set(spec["components"]["schemas"].get("QCSRead", {}).get("properties", {}))
check("QCSRead exposes the D59 fields",
      {"recommendation", "levels_required", "levels_approved", "justification_min_length", "created_by"} <= read_props)

print("--- older routes still there")
check("POST /api/rfq/generate", "post" in paths.get("/api/rfq/generate", {}))
check("POST /api/rfq/{rfq_id}/close", "post" in paths.get("/api/rfq/{rfq_id}/close", {}))
check("a comparison route still exists", any(p.endswith("/comparison") for p in paths))

pairs = [(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods]
check("no route is registered twice (method + path)", len(pairs) == len(set(pairs)))

print(f"\n{passed}/{passed + failed} checks passed")
print("ALL PASS" if failed == 0 else "SOME FAILED")