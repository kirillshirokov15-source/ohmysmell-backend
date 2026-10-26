"""Order 9's uncertain reserve is recovered ONLY from a verified GET result."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func

from tests.test_tilda_sku_review import database, catalog, tilda_config, payload
from app.config.settings import settings
from app.models.order import Order
from app.models.fulfillment import Shipment, WarehouseAllocation, ExternalOperation
from app.models.operations import OrderEvent
from app.models.order_desk import DeskMessage
from app.services.order_desk import DeskError, TildaIntake, DeskService
from app.services.warehouse_documents import plan_key
from app.services.own_stock_reconciliation import (
    OwnReservationReconciler, OwnReservationReconciliationError, reserve_intent,
)
from app.integrations.moysklad.documents import FakeDocumentAdapter

CHAT=-100123456


@pytest.fixture
def prepared_own_reservation(database, catalog, monkeypatch):
    monkeypatch.setattr(settings,"environment","production")
    monkeypatch.setattr(settings,"external_writes_enabled",True)
    monkeypatch.setattr(settings,"moysklad_organization_id","33333333-3333-3333-3333-333333333333")
    monkeypatch.setattr("app.services.external_operation_service.async_session",database)

    async def prepare():
        did=(await TildaIntake().submit(payload()))["draft_id"]
        await DeskService().claim(did,111,CHAT)
        order=await DeskService().confirm(did,111,CHAT,0)
        async with database() as s,s.begin():
            live=await s.get(Order,order.id)
            live.counterparty_id="22222222-2222-2222-2222-222222222222"
            live.fulfillment_status="assembled"
            plan=Shipment(order_id=order.id,warehouse_id="11111111-1111-1111-1111-111111111111",
                          status="planned")
            plan.allocations.append(WarehouseAllocation(order_item_id=order.items[0].id,qty=1,
                managed_source=True,source_type="own",supply_status="confirmed"))
            s.add(plan)
            await s.flush()
            await s.refresh(live, ["items"])
            await s.refresh(plan, ["allocations"])
            key=plan_key(plan,"reserve",False)
            # The actual method generates the same payload from order data.
            intent=reserve_intent(live,plan)
            s.add(ExternalOperation(key=key,payload=intent,status="uncertain"))
            return order.id,plan.id,key,intent,did

    return database,asyncio.run(prepare())


def verified_fake(intent):
    adapter=FakeDocumentAdapter()
    adapter.documents[intent["document"]["externalCode"]]={
        **deepcopy(intent["document"]), "id":"verified-provider-reserve-id"}
    return adapter


def test_uncertain_reserve_reconciles_existing_only(prepared_own_reservation):
    sessions,(oid,pid,key,intent,did)=prepared_own_reservation
    adapter=verified_fake(intent)
    service=OwnReservationReconciler(sessions=sessions,adapter=adapter)
    async def run():
        async with sessions() as s:
            before=await s.scalar(select(func.count()).select_from(DeskMessage))
            old=(await s.get(Order,oid)).revision
        result=await service.reconcile(oid,pid,111,CHAT)
        assert result=={"status":"reconciled","id":"verified-provider-reserve-id"}
        assert adapter.calls==0
        async with sessions() as s:
            assert (await s.get(ExternalOperation,key)).status=="succeeded"
            event=await s.scalar(select(OrderEvent).where(OrderEvent.idempotency_key==key))
            assert event.action=="warehouse_reserve"
            assert (await s.get(Order,oid)).revision==old+1
            assert await s.scalar(select(func.count()).select_from(DeskMessage))==before
        again=await service.reconcile(oid,pid,111,CHAT)
        assert again["status"]=="already_verified"
        assert adapter.calls==0
    asyncio.run(run())


@pytest.mark.parametrize("mode",["missing","multiple","mismatch"])
def test_no_duplicate_provider_write_and_no_mutations_when_unverified(prepared_own_reservation,mode):
    sessions,(oid,pid,key,intent,did)=prepared_own_reservation
    adapter=verified_fake(intent)
    record=adapter.documents[intent["document"]["externalCode"]]
    if mode=="missing":
        adapter.documents.clear()
    elif mode=="multiple":
        adapter.find=AsyncMock(return_value=[record,deepcopy(record)])
    else:
        record["agent"]["meta"]["href"]="https://api.moysklad.ru/api/remap/1.2/entity/counterparty/not-buyer"
    service=OwnReservationReconciler(sessions=sessions,adapter=adapter)
    async def run():
        expected = {"missing":"документов не найдено",
                    "multiple":"Неоднозначно",
                    "mismatch":"покупатель отличается"}[mode]
        with pytest.raises(OwnReservationReconciliationError, match=expected):
            await service.reconcile(oid,pid,111,CHAT)
        assert adapter.calls==0
        async with sessions() as s:
            assert (await s.get(ExternalOperation,key)).status=="uncertain"
            assert await s.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.idempotency_key==key))==0
    asyncio.run(run())


def test_existing_succeeded_journal_without_audit_is_repairable(prepared_own_reservation):
    sessions,(oid,pid,key,intent,did)=prepared_own_reservation
    adapter=verified_fake(intent)
    async def run():
        async with sessions() as s,s.begin():
            record=await s.get(ExternalOperation,key)
            record.status="succeeded"
            record.result=deepcopy(adapter.documents[intent["document"]["externalCode"]])
        recovered=await OwnReservationReconciler(sessions=sessions,adapter=adapter).reconcile(
            oid,pid,111,CHAT)
        assert recovered["status"]=="reconciled"
        assert adapter.calls==0
        async with sessions() as s:
            assert await s.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.idempotency_key==key))==1
    asyncio.run(run())
