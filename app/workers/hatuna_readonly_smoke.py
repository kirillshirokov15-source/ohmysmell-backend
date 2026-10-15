"""Standalone staging-only, read-only Hatuna counterparty API verification.

This intentionally runs a static HTTP server, NOT the Telegram poller. It has
no method or code path that can create MoySklad or CRM documents.
"""
import asyncio
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

from app.services.hatuna_accounting import verify_hatuna_counterparty


async def _check():
    return await verify_hatuna_counterparty(os.getenv("OMS_HATUNA_COUNTERPARTY_ID", ""))


async def _candidate_orders_readonly():
    """List only staging CRM order IDs eligible for independent X preflight.

    No customer data, no provider writes, no order changes, no simulation.
    Candidate means assembled with X allocation, NOT cleared to post stock.
    """
    from sqlalchemy import select
    from app.database.session import async_session
    from app.models.order import Order
    from app.models.fulfillment import Shipment, WarehouseAllocation
    from app.config.settings import settings

    if (settings.external_writes_enabled
            or os.getenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false").lower() == "true"
            or os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() == "true"):
        print("OMS_X_CANDIDATES:SKIPPED_UNSAFE_FLAGS", flush=True)
        return
    async with async_session() as session:
        ids = list((await session.scalars(
            select(Order.id)
            .join(Shipment, Shipment.order_id == Order.id)
            .join(WarehouseAllocation, WarehouseAllocation.shipment_id == Shipment.id)
            .where(Order.source == "website",
                   Order.manual_fulfillment.is_(True),
                   Order.fulfillment_status == "assembled",
                   Order.needs_review.is_(False),
                   WarehouseAllocation.source_type == "partner_x",
                   WarehouseAllocation.supply_status != "cancelled")
            .distinct().order_by(Order.id.desc()).limit(10)
        )).all())
    print("OMS_X_CANDIDATES:" + (",".join(str(i) for i in ids) or "NONE"), flush=True)


def main():
    if os.getenv("APP_ENV") != "staging":
        raise RuntimeError("Hatuna read-only smoke is staging-only")
    print("HATUNA_READONLY_CHECK_STARTED", flush=True)
    try:
        asyncio.run(_check())
    except Exception as exc:
        # No tokens, names, emails, URLs or other personal/provider data.
        print(f"HATUNA_READONLY_CHECK_FAILED:{type(exc).__name__}", flush=True)
    else:
        print("HATUNA_READONLY_CHECK_VERIFIED", flush=True)
        try:
            from app.integrations.moysklad.client import MoySkladClient
            client = MoySkladClient()
            warehouses = client.get_stores()
            organizations = client.get_organizations()
            def safe_name(value):
                return " ".join((value or "").split())[:65]
            for warehouse in warehouses[:30]:
                name = safe_name(warehouse.get("name"))
                kind = "X" if name.casefold().replace(" ", "").replace("x", "х") == "складх" else "OTHER"
                if not warehouse.get("archived"):
                    print(f"OMS_READONLY_WAREHOUSE:{kind}:{warehouse.get('id')}:{name}", flush=True)
            for org in organizations[:15]:
                if not org.get("archived"):
                    print(f"OMS_READONLY_ORGANIZATION:{org.get('id')}:{safe_name(org.get('name'))}", flush=True)
        except Exception as exc:
            print(f"OMS_READONLY_ACCOUNT_REFERENCES_FAILED:{type(exc).__name__}", flush=True)

    try:
        asyncio.run(_candidate_orders_readonly())
    except Exception as exc:
        print(f"OMS_X_CANDIDATES_ERROR:{type(exc).__name__}", flush=True)

    # Railway's staging manager service is intentionally a harmless HTTP
    # smoke container until a separate safe acceptance is completed.
    HTTPServer(("0.0.0.0", 8080), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
