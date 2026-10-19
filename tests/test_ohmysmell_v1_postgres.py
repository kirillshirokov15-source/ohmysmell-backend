"""Full document workflow against isolated PostgreSQL and fake providers only."""
import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from tests.test_order_sources_postgres import sources_postgres,desk_db,staging_sessions,tilda_config
from tests.test_order_sources import seed_sources,CHAT
from app.models.order import Order
from app.models.supply import Supplier
from app.models.fulfillment import Shipment,ExternalOperation
from app.models.buying import BuyingPurchase
from app.services import buying
from app.services.warehouse_documents import WarehouseDocumentService
from app.services.order_source_service import source_plans
from app.services.order_operations import OrderOperations,OrderAction,OperationError
from app.services.order_desk import DeskError
from app.schemas.order_sources import SourceAction
from app.integrations.moysklad.documents import FakeDocumentAdapter
from app.config.settings import settings

pytestmark=pytest.mark.staging


def test_received_sources_reserve_ship_documents(sources_postgres,monkeypatch):
    factory,engine,actor,pid=sources_postgres
    monkeypatch.setattr(settings,'moysklad_organization_id','synthetic-org')
    async def run():
        order,source,parts=await seed_sources(factory,actor,pid=pid)
        monkeypatch.setattr('app.services.product_service.ProductService.get_catalog_async',source.products.get_catalog_async)
        async with factory() as s,s.begin():
            (await s.get(Order,order.id)).counterparty_id='synthetic-customer'
            suppliers=(await s.scalars(select(Supplier).where(Supplier.id.in_([parts[2]['supplier_id'],
                (await s.get(__import__('app.models.supply',fromlist=['SupplierOffer']).SupplierOffer,parts[4]['offer_id'])).supplier_id])))).all()
            for supplier in suppliers: supplier.external_reference='supplier-'+str(supplier.id)
        view=await source.act(order.id,actor,CHAT,SourceAction(action='assign',parts=parts,expected_revision=0,idempotency_key=uuid4().hex))
        adapter=FakeDocumentAdapter(); docs=WarehouseDocumentService(adapter=adapter,sessions=factory)
        for allocation in view['allocations']:
            if allocation['source_type']=='own': continue
            if allocation['purchase_id']:
                async with factory() as s,s.begin():
                    await buying.simulate_send(s,allocation['purchase_id'],'test')
                    await buying.confirm_supplier(s,allocation['purchase_id'],'test')
                    await buying.receive(s,allocation['purchase_id'],'test')
            with pytest.raises(DeskError):
                await docs.act(order.id,actor,CHAT,view['revision'],'receipt',allocation['id'])
            view=await source.act(order.id,actor,CHAT,SourceAction(action='receive',allocation_id=allocation['id'],
                expected_revision=view['revision'],idempotency_key=uuid4().hex))
            result=await docs.act(order.id,actor,CHAT,view['revision'],'receipt',allocation['id'])
            assert result==await docs.act(order.id,actor,CHAT,view['revision'],'receipt',allocation['id'])
            view=await source.read(order.id,actor,CHAT)
        async with factory() as s: plans=await source_plans(s,order.id)
        for plan in plans:
            await docs.act(order.id,actor,CHAT,view['revision'],'reserve',plan.id)
            view=await source.read(order.id,actor,CHAT)
        with pytest.raises(DeskError):
            await source.act(order.id,actor,CHAT,SourceAction(action='cancel',allocation_id=view['allocations'][0]['id'],
                expected_revision=view['revision'],idempotency_key=uuid4().hex))
        ops=OrderOperations()
        for action in ('assembling','assembled'):
            current=await ops.act(order.id,actor,OrderAction(action=action,expected_revision=view['revision'],idempotency_key=uuid4().hex))
            view=await source.read(order.id,actor,CHAT)
        for plan in plans:
            await docs.act(order.id,actor,CHAT,view['revision'],'ship',plan.id)
            view=await source.read(order.id,actor,CHAT)
        current=await ops.act(order.id,actor,OrderAction(action='delivery',delivery_method='manual',expected_revision=view['revision'],idempotency_key=uuid4().hex))
        for action in ('shipped','paid'):
            current=await ops.act(order.id,actor,OrderAction(action=action,expected_revision=current.revision,idempotency_key=uuid4().hex))
        assert current.fulfillment_status=='shipped' and current.payment_status=='paid'
        current=await ops.act(order.id,actor,OrderAction(action='delivery',delivery_status='delivered',
            expected_revision=current.revision,idempotency_key=uuid4().hex))
        assert current.delivery_status=='delivered' and current.payment_status=='paid'
        from app.services.manager_workspace import order_card
        assert 'Статус: Доставлен' in order_card(current)
        assert 'Оплата: Оплачен' in order_card(current)
        assert adapter.calls==7  # 3 receipts, 2 warehouse reservations, 2 demands
        assert sum(p['quantity'] for d in adapter.documents.values() if 'customerOrder' in d for p in d['positions'])==8
        assert all(d['simulated'] for d in adapter.documents.values())
        await engine.dispose()
    asyncio.run(run())


def test_unknown_reservation_release_and_cancel(sources_postgres,monkeypatch):
    factory,engine,actor,pid=sources_postgres
    monkeypatch.setattr(settings,'moysklad_organization_id','synthetic-org')
    async def run():
        order,source,parts=await seed_sources(factory,actor,pid=pid)
        monkeypatch.setattr('app.services.product_service.ProductService.get_catalog_async',source.products.get_catalog_async)
        async with factory() as s,s.begin():
            (await s.get(Order,order.id)).counterparty_id='synthetic-customer'
        view=await source.act(order.id,actor,CHAT,SourceAction(action='assign',parts=parts[:1],
            expected_revision=0,idempotency_key=uuid4().hex))
        allocation=view['allocations'][0]; plan=allocation['plan_id']
        adapter=FakeDocumentAdapter(); adapter.lose_response=True
        docs=WarehouseDocumentService(adapter=adapter,sessions=factory)
        with pytest.raises(TimeoutError):
            await docs.act(order.id,actor,CHAT,view['revision'],'reserve',plan)
        with pytest.raises(DeskError):
            await source.act(order.id,actor,CHAT,SourceAction(action='cancel',allocation_id=allocation['id'],
                expected_revision=view['revision'],idempotency_key=uuid4().hex))
        result=await docs.act(order.id,actor,CHAT,view['revision'],'reserve',plan,reconcile=True)
        adapter.lose_response=False
        released=await docs.act(order.id,actor,CHAT,result['revision'],'release',plan)
        assert adapter.calls==2
        with pytest.raises(DeskError):
            await docs.act(order.id,actor,CHAT,released['revision'],'reserve',plan)
        view=await source.act(order.id,actor,CHAT,SourceAction(action='cancel',allocation_id=allocation['id'],
            expected_revision=released['revision'],idempotency_key=uuid4().hex))
        with pytest.raises(DeskError):
            await docs.act(order.id,actor,CHAT,view['revision'],'ship',plan)
        await engine.dispose()
    asyncio.run(run())


def test_provider_reservation_is_not_counted_twice(sources_postgres):
    factory,engine,actor,pid=sources_postgres
    from app.services.order_source_service import claimed_stock
    from app.services.warehouse_documents import plan_key
    async def run():
        order,source,parts=await seed_sources(factory,actor,pid=pid)
        await source.act(order.id,actor,CHAT,SourceAction(action='assign',parts=parts[:1],
            expected_revision=0,idempotency_key=uuid4().hex))
        async with factory() as s,s.begin():
            plan=(await source_plans(s,order.id))[0]
            assert await claimed_stock(s,pid,'wh1')==2
            record=ExternalOperation(key=plan_key(plan,'reserve',False),payload={},status='uncertain')
            s.add(record); await s.flush()
            assert await claimed_stock(s,pid,'wh1')==2
            record.status='succeeded'; record.result={'id':'synthetic-reserve'}; await s.flush()
            assert await claimed_stock(s,pid,'wh1')==0
            s.add(ExternalOperation(key=plan_key(plan,'release',False),payload={},status='uncertain'))
            await s.flush()
            assert await claimed_stock(s,pid,'wh1')==2
        await engine.dispose()
    asyncio.run(run())


def test_standalone_buying_receipt_cannot_be_received_again(sources_postgres,monkeypatch):
    factory,engine,actor,pid=sources_postgres
    monkeypatch.setattr(settings,'moysklad_organization_id','synthetic-org')
    async def run():
        order,source,parts=await seed_sources(factory,actor,pid=pid)
        monkeypatch.setattr('app.services.product_service.ProductService.get_catalog_async',source.products.get_catalog_async)
        from app.models.supply import SupplierOffer
        async with factory() as s,s.begin():
            offer=await s.get(SupplierOffer,parts[-1]['offer_id'])
            supplier=await s.get(Supplier,offer.supplier_id); supplier.external_reference='synthetic-supplier'
            purchase=await buying.create_purchase(s,dict(supplier_id=supplier.id,supplier_name='Synthetic',
                recipient='supplier@example.invalid',currency='RUB',total_minor=270000,approximate=False,
                approximate_rub_minor=270000,email_body='Item 3',items=[dict(offer_id=offer.id,product_id=pid,
                name='Item',quantity=3,unit_price_minor=90000)]),'test')
            await buying.simulate_send(s,purchase.id,'test')
            await buying.confirm_supplier(s,purchase.id,'test')
        adapter=FakeDocumentAdapter(); docs=WarehouseDocumentService(adapter=adapter,sessions=factory)
        with pytest.raises(DeskError):
            await docs.receive_purchase(purchase.id,'test','wh1')
        async with factory() as s,s.begin():
            await buying.receive(s,purchase.id,'test')
        adapter.lose_response=True
        with pytest.raises(TimeoutError):
            await docs.receive_purchase(purchase.id,'test','wh1')
        with pytest.raises(DeskError):
            await source.act(order.id,actor,CHAT,SourceAction(action='assign',parts=[{**parts[-1],'purchase_id':purchase.id}],
                expected_revision=0,idempotency_key=uuid4().hex))
        with pytest.raises(DeskError):
            await docs.receive_purchase(purchase.id,'test','wh2',reconcile=True)
        receipt=await docs.receive_purchase(purchase.id,'test','wh1',reconcile=True)
        assert receipt==await docs.receive_purchase(purchase.id,'test','wh1')
        assert adapter.calls==1
        with pytest.raises(DeskError):
            await source.act(order.id,actor,CHAT,SourceAction(action='assign',parts=[{**parts[-1],'purchase_id':purchase.id}],
                expected_revision=0,idempotency_key=uuid4().hex))
        with pytest.raises(DeskError):
            await docs.receive_purchase(purchase.id,'test','wh2')
        assert adapter.calls==1
        await engine.dispose()
    asyncio.run(run())
