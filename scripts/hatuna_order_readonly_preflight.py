"""Read-only preflight for ONE selected Tilda order before enabling X accounting.

Requires full assembly first. No adapter.create, no external mutation, no
Telegram interactions, no Gmail sending. Output contains no customer PII.
"""
import argparse
import asyncio
import json
import os

from app.config.settings import settings
from app.integrations.moysklad.documents import FakeDocumentAdapter
from app.services.hatuna_accounting import HatunaPreflightError
from app.services.hatuna_fulfillment import HatunaFulfillment


async def inspect(order_id: int):
    if order_id <= 0:
        raise ValueError("order_id must be positive")
    if settings.external_writes_enabled:
        raise RuntimeError("Run preflight only with EXTERNAL_WRITES_ENABLED=false")
    if os.getenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false").lower() == "true":
        raise RuntimeError("Live Hatuna writes must be off for read-only preflight")
    if os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() == "true":
        raise RuntimeError("Stop auto-processing before independent preflight")
    # This is a local flag on this inspection process, NOT a Railway change.
    os.environ["OMS_HATUNA_POST_PICK_ENABLED"] = "true"
    service = HatunaFulfillment(adapter=FakeDocumentAdapter())
    try:
        order, plans, x_plans, items, actor, chat = await service.preflight(order_id)
    except HatunaPreflightError as error:
        return {"order_id": order_id, "status": "BLOCKED",
                "reason": str(error)[:700], "no_external_writes": True}
    return {
        "order_id": order_id, "status": "READY_FOR_CONTROLLED_TRIAL",
        "warehouses": len(plans), "x_warehouse_plans": len(x_plans),
        "order_items": len(items), "no_external_writes": True,
        "live_posting_enabled": False,
        "note": "Preflight only; does not mean documents were created",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", required=True, type=int)
    args = parser.parse_args()
    result = asyncio.run(inspect(args.order_id))
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "BLOCKED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
