"""Idempotent Hatuna PO -> linked supply executor.

The caller must obtain a trusted, committed picker confirmation and an exact
provider catalog/warehouse preflight BEFORE invoking. This component never
marks an order shipped; the separate warehouse reserve/shipment stage follows.
"""
import os

from app.config.settings import settings
from app.integrations.moysklad.documents import FakeDocumentAdapter, MoySkladDocumentAdapter
from app.services.external_operation_service import ExternalOperationService
from app.services.hatuna_accounting import (
    HatunaPreflightError, hatuna_operation_keys, purchase_intent,
    receipt_intent,
)


class HatunaDocumentRunner:
    def __init__(self, *, adapter=None, operations=None):
        self.adapter = adapter if adapter is not None else MoySkladDocumentAdapter()
        self.operations = operations if operations is not None else ExternalOperationService()

    async def apply(self, *, order_id, x_plan_id, organization_id, hatuna_id,
                    x_store_id, items, picker_confirmed):
        fake = type(self.adapter) is FakeDocumentAdapter
        if fake:
            if settings.environment == "production":
                raise HatunaPreflightError("Тестовые документы в production запрещены")
        else:
            # In addition to global MoySklad writes, Hatuna has an independent
            # disabled-by-default live gate: the old enter() is not reused.
            if (settings.environment != "production"
                    or not settings.external_writes_enabled
                    or os.getenv("OMS_HATUNA_AUTO_WRITES_ENABLED", "false").lower() != "true"):
                raise HatunaPreflightError(
                    "Реальная автоматизация Хатуны ещё не активирована"
                )
        request = dict(order_id=order_id, x_plan_id=x_plan_id,
                       organization_id=organization_id, hatuna_id=hatuna_id,
                       x_store_id=x_store_id, items=items,
                       picker_confirmed=picker_confirmed)
        # Compute and validate both price and mapping before any external I/O.
        po_intent = purchase_intent(**request)
        keys = hatuna_operation_keys(order_id, x_plan_id)
        if fake:
            purchase = await self.operations.simulate_document(
                keys["purchase"], po_intent, self.adapter)
        else:
            purchase = await self.operations.execute(
                keys["purchase"], po_intent, self.adapter.create)
        # If a provider call's outcome is uncertain, the external operation
        # ledger blocks replay until reconciliation. Never send the receipt.
        receipt = receipt_intent(**request, purchase_id=purchase["id"],
                                simulation=fake)
        if fake:
            received = await self.operations.simulate_document(
                keys["receipt"], receipt, self.adapter)
        else:
            received = await self.operations.execute(
                keys["receipt"], receipt, self.adapter.create)
        return dict(
            purchase_order_id=purchase["id"],
            receipt_id=received["id"],
            simulated=fake,
            provisional_cost=True,
            purchase_key=keys["purchase"],
            receipt_key=keys["receipt"],
        )
