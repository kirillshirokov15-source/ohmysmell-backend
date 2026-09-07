"""Controlled live-read MoySklad -> FakeEmail -> local staging order E2E."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4


async def run():
    from sqlalchemy import select
    from app.database.session import async_session, engine
    from app.models.customer import Customer
    from app.models.sales import CustomerType
    from app.integrations.moysklad.client import MoySkladClient
    from app.integrations.email.provider import EmailMessage, FakeEmailProvider
    from app.services.product_service import ProductService
    from app.services.draft_order_service import DraftOrderService
    from app.services.fulfillment_service import FulfillmentService
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.workers.email_ingestion import EmailIngestionWorker
    from app.services.draft_telegram_service import notify_managers_about_draft
    from app.config.settings import settings
    assert not settings.external_writes_enabled
    report = {"environment": "staging", "external_writes": False, "run_id": uuid4().hex}
    client = MoySkladClient()
    products = ProductService(client=client)
    start = perf_counter()
    catalog = await products.get_catalog_async(customer_type=CustomerType.WHOLESALE)
    report["catalog_duration_ms"] = round((perf_counter() - start) * 1000, 2)
    report["catalog_products"] = len(catalog)
    report["warehouse_count"] = len({s["id"] for p in catalog for s in p["stocks"]})
    report["organizations_count"] = len(await asyncio.to_thread(client.get_organizations))
    report["price_types_count"] = len(await asyncio.to_thread(client.get_price_types))
    selected = [p for p in catalog if p["price"] is not None and p["total_available"] >= 1][:2]
    if len(selected) < 2:
        raise RuntimeError("Two priced in-stock products are required")
    counterparties = await asyncio.to_thread(client.get_recent_counterparties, 100)
    async with async_session() as session:
        used = set((await session.execute(select(Customer.moysklad_counterparty_id))).scalars())
    counterparty = next((c for c in counterparties if c["id"] not in used and not c.get("archived")), None)
    if not counterparty:
        raise RuntimeError("No unused existing counterparty for isolated staging customer")
    message = EmailMessage(external_message_id="audit-e2e:" + report["run_id"],
        sender_email="audit-" + report["run_id"] + "@example.invalid",
        sender_name="STAGING AUDIT TEST", subject="STAGING AUDIT — NO EXTERNAL ORDER",
        body_text="\n".join((p.get("article") or p["name"]) + " x1" for p in selected),
        received_at=datetime.now(timezone.utc))
    # One final-state notification only, clearly marked as staging test data.
    async def defer_notification(draft):
        raise RuntimeError("Deferred until staging E2E completes")
    pipeline = DraftOrderService(notifier=defer_notification)
    worker = EmailIngestionWorker(FakeEmailProvider([message]), pipeline, provider_name="audit_fake")
    start = perf_counter()
    report["email_stats"] = await worker.run_once()
    draft = await DraftOrderRepository().get_by_external_message_id(message.external_message_id)
    if draft is None:
        raise RuntimeError("Draft ingestion failed")
    report["draft_id"] = draft.id
    report["ingestion_duration_ms"] = round((perf_counter() - start) * 1000, 2)
    await pipeline.set_customer_type(draft.id, CustomerType.WHOLESALE)
    for item, product in zip(draft.items, selected):
        await pipeline.resolve_product(draft.id, item.id, product["id"])
    ready = await pipeline.link_counterparty(draft.id, counterparty["id"], counterparty.get("name") or "Existing")
    assert ready.status == "ready"
    start = perf_counter()
    order = await pipeline.finalize(draft.id)
    again = await pipeline.finalize(draft.id)
    assert order.id == again.id and order.moysklad_order_id is None
    assert order.total == sum(p["price"] for p in selected)
    report.update(order_id=order.id, total_minor=order.total, duplicate_finalize_same_order=True,
                  finalize_duration_ms=round((perf_counter() - start) * 1000, 2))
    shipments = await FulfillmentService(products).plan(order.id)
    report["planned_shipments"] = len(shipments)
    assert sum(a.qty for s in shipments for a in s.allocations) == 2
    final_draft = await DraftOrderRepository().get(draft.id)
    try:
        report["telegram_notifications"] = await notify_managers_about_draft(final_draft)
        await DraftOrderRepository().mark_notified(draft.id)
    except Exception as error:
        report["telegram_error_type"] = type(error).__name__
    report["passed"] = True
    Path(".staging-artifacts/e2e.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    await engine.dispose()


def main():
    from app.api.staging_runner import configure
    configure()
    try:
        asyncio.run(run())
    except Exception as error:
        print("STAGING E2E failed: " + type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
