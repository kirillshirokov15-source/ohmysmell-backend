"""Exercise the ASGI HTTP contract with real staging DB and read-only catalog."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4


async def run():
    from app.main import app
    from app.config.settings import settings
    from app.database.session import engine
    from tests.asgi_client import request
    report = {"environment": "staging", "external_writes": settings.external_writes_enabled}
    code, catalog, _ = await request(app, "GET", "/api/v1/catalog")
    assert code == 200
    product = next(p for p in catalog["products"] if p["available"])
    key = uuid4().hex
    payload = {"customer_name": "STAGING WEBSITE TEST", "email": key + "@example.invalid",
               "phone": "+79990000000", "items": [{"product_id": product["id"], "qty": 1}]}
    headers = {"Idempotency-Key": key}
    first_code, first, _ = await request(app, "POST", "/api/v1/checkout", payload, headers)
    second_code, second, _ = await request(app, "POST", "/api/v1/checkout", payload, headers)
    assert first_code == second_code == 202 and first == second
    assert first["status"] == "needs_review" and first["total_minor"] is None
    conflict, _, _ = await request(app, "POST", "/api/v1/checkout", {**payload, "comment": "changed"}, headers)
    assert conflict == 409
    denied, _, _ = await request(app, "GET", "/orders")
    allowed, _, _ = await request(app, "GET", "/orders", headers={"X-Internal-API-Token": settings.internal_api_token})
    assert denied == 401 and allowed == 200
    report.update(catalog_status=code, total_products=catalog["total"], checkout_status=first_code,
        retail_total_minor=first["total_minor"], replay_identical=True, conflict_status=conflict,
        unauthorized_status=denied, authorized_status=allowed, request_id=key, passed=True)
    Path(".staging-artifacts/api-e2e.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    await engine.dispose()


if __name__ == "__main__":
    from app.api.staging_runner import configure
    configure()
    try:
        asyncio.run(run())
    except Exception as error:
        print("STAGING API E2E failed: " + type(error).__name__)
        raise SystemExit(1)
