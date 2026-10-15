"""Replays assembled Tilda orders through journaled warehouse documents.

Enabled only explicitly. A failed/uncertain operation never retries an external
create: the operation ledger blocks it and requires reconciliation.
"""
import asyncio
import logging
import os

from sqlalchemy import select
from app.database.session import async_session
from app.models.order import Order
from app.services.hatuna_fulfillment import HatunaFulfillment

logger = logging.getLogger(__name__)


async def run_once(service=None, session_factory=None):
    if os.getenv("OMS_HATUNA_AUTO_PROCESS_ENABLED", "false").lower() != "true":
        return 0
    if os.getenv("OMS_HATUNA_POST_PICK_ENABLED", "false").lower() != "true":
        raise RuntimeError("Hatuna worker requires post-picking workflow")
    executor = service or HatunaFulfillment()
    factory = session_factory or async_session
    async with factory() as session:
        ids = list((await session.scalars(
            select(Order.id).where(Order.manual_fulfillment.is_(True),
                Order.source == "website",
                Order.fulfillment_status == "assembled",
                Order.needs_review.is_(False))
            .order_by(Order.id).limit(15))).all())
    # Run serially. DB and ExternalOperation locks coordinate restarts and
    # stop duplicate purchase/receipt/reserve/ship if polling overlaps.
    completed = 0
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    for order_id in ids:
        if not post_pick_enabled_for_order(order_id):
            continue
        try:
            await executor.run_order(order_id)
            completed += 1
        except Exception as error:
            # No customer data, IDs in URLs or token-bearing payloads in logs.
            logger.warning("hatuna_auto_order_pending order_id=%s reason=%s",
                           order_id, type(error).__name__)
    return completed


async def main():
    while True:
        try:
            await run_once()
        except Exception as error:
            logger.error("hatuna_worker_cycle_failed reason=%s", type(error).__name__)
        await asyncio.sleep(10)
