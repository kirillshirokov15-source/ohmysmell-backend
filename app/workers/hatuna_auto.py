"""Automatic X-only post-pick accounting for NEW, explicitly scoped production orders.

One manager worker owns polling. The provider journal owns individual document
idempotency; unsuccessful or uncertain calls must never be silently retried.
Existing orders remain outside the activation timestamp by default.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select, exists
from app.config.settings import settings
from app.database.session import async_session
from app.models.order import Order
from app.models.fulfillment import Shipment, WarehouseAllocation
from app.services.hatuna_fulfillment import HatunaFulfillment

logger = logging.getLogger(__name__)


def activation_time():
    """In production an explicit UTC-aware cutoff is mandatory; no retroactive sweep."""
    if settings.environment != "production":
        return None
    value = os.getenv("OMS_HATUNA_AUTO_NOT_BEFORE", "").strip()
    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("X auto-receipt requires valid OMS_HATUNA_AUTO_NOT_BEFORE") from exc
    if start.tzinfo is None:
        raise RuntimeError("X auto-receipt requires timezone-aware activation time")
    return start.astimezone(timezone.utc)


def require_live_gates():
    if settings.environment != "production":
        return None
    if not settings.external_writes_enabled:
        raise RuntimeError("X auto-receipt requires external writes for the manager worker")
    for key in ("OMS_HATUNA_AUTO_WRITES_ENABLED", "OMS_OWN_X_LIVE_ENABLED"):
        if os.getenv(key, "false").lower() != "true":
            raise RuntimeError("X auto-receipt is not enabled: " + key)
    if os.getenv("OMS_X_COST_POLICY", "") != "provisional_minus_1000":
        raise RuntimeError("Explicit X provisional accounting policy is required")
    return activation_time()


async def run_once(service=None, session_factory=None):
    if os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() != "true":
        return 0
    if os.getenv("OMS_HATUNA_POST_PICK_ENABLED", "false").lower() != "true":
        raise RuntimeError("X automatic worker requires post-pick workflow")

    earliest = require_live_gates()
    executor = service or HatunaFulfillment()
    factory = session_factory or async_session
    from app.services.hatuna_post_pick import post_pick_enabled_for_order

    # Include only allocated X lines, not own-only assembled orders. Process
    # bounded pages: one problematic old order cannot starve later valid orders.
    has_x = exists(
        select(WarehouseAllocation.id)
        .join(Shipment, WarehouseAllocation.shipment_id == Shipment.id)
        .where(
            Shipment.order_id == Order.id,
            Shipment.status.in_(("planned", "exported")),
            WarehouseAllocation.managed_source.is_(True),
            WarehouseAllocation.source_type == "partner_x",
            WarehouseAllocation.supply_status != "cancelled",
        )
    )
    processed, last_id = 0, 0
    for _ in range(10):
        async with factory() as session:
            statement = select(Order.id).where(
                Order.id > last_id,
                Order.manual_fulfillment.is_(True),
                Order.source.in_(("website", "email")),
                Order.fulfillment_status == "assembled",
                Order.needs_review.is_(False),
                has_x,
            )
            if earliest is not None:
                # Requiring both timestamps prevents old orders assembled after
                # activation from silently creating real documents.
                statement = statement.where(
                    Order.created_at >= earliest,
                    Order.assembled_at >= earliest,
                )
            batch = list((await session.scalars(
                statement.order_by(Order.id).limit(40)
            )).all())
        if not batch:
            break
        for order_id in batch:
            if not post_pick_enabled_for_order(order_id):
                continue
            try:
                await executor.run_order(order_id)
                processed += 1
            except Exception as error:
                logger.warning(
                    "x_auto_order_pending order_id=%s reason=%s",
                    order_id, type(error).__name__,
                )
        last_id = batch[-1]
        if len(batch) < 40:
            break
    return processed


async def main():
    while True:
        try:
            await run_once()
        except Exception as error:
            logger.error("x_auto_cycle_failed reason=%s", type(error).__name__)
        await asyncio.sleep(20)


if __name__ == "__main__":
    asyncio.run(main())
