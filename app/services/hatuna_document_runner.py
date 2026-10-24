"""Durable, idempotent ONE-document Hatuna internal stock entry.

After a picker has confirmed Tilda goods and verified catalog/stock, create
an `enter` document with estimated cost. No supplier order, supply, email,
supplier payable, or counterparty relationship is created.
"""
import os

from app.config.settings import settings
from app.integrations.moysklad.documents import FakeDocumentAdapter, MoySkladDocumentAdapter
from app.services.external_operation_service import ExternalOperationService
from app.services.hatuna_accounting import (
    HatunaPreflightError, hatuna_operation_keys, stock_entry_intent,
)


class HatunaDocumentRunner:
    def __init__(self, *, adapter=None, operations=None, live_preflight=None):
        self.adapter = adapter if adapter is not None else MoySkladDocumentAdapter()
        self.operations = operations if operations is not None else ExternalOperationService()
        self.live_preflight = live_preflight

    async def apply(self, *, order_id, x_plan_id, organization_id,
                    x_store_id, items, picker_confirmed):
        fake = type(self.adapter) is FakeDocumentAdapter
        if fake:
            if settings.environment == "production":
                raise HatunaPreflightError("Симуляция в production запрещена")
        else:
            if (settings.environment != "production"
                    or not settings.external_writes_enabled
                    or os.getenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false").lower() != "true"):
                raise HatunaPreflightError("Реальное оприходование Хатуны пока не активировано")
            # The explicit pricing model is provisional accounting, not a
            # factual supplier cost. Require an independent operator switch.
            if self.live_preflight is None:
                raise HatunaPreflightError("Нет повторной сверки заказа и остатков МойСклад")
            if (os.getenv("OMS_OWN_X_LIVE_ENABLED", "false").lower() != "true"
                    or os.getenv("OMS_X_COST_POLICY", "") != "provisional_minus_1000"):
                raise HatunaPreflightError("Не разрешена предварительная оценка для склада X")
        request = dict(
            order_id=order_id, x_plan_id=x_plan_id,
            organization_id=organization_id, x_store_id=x_store_id,
            items=items, picker_confirmed=picker_confirmed,
        )
        intent = stock_entry_intent(**request)
        if not fake:
            # Re-check saved source allocations and fresh provider stock.
            await self.live_preflight(request)
        key = hatuna_operation_keys(order_id, x_plan_id)["receipt"]
        if fake:
            created = await self.operations.simulate_document(key, intent, self.adapter)
        else:
            created = await self.operations.execute(key, intent, self.adapter.create)
        # If outcome is uncertain the journal refuses blind retries.
        return dict(receipt_id=created["id"], simulated=fake,
                    provisional_cost=True, receipt_key=key)
