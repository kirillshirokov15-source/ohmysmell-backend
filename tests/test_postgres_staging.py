"""Opt in through scripts.run_staging_tests; no production connection fallback."""
import asyncio
import importlib
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

pytestmark = pytest.mark.staging


def test_quantity_review_and_correction_postgres(staging_sessions):
    from app.models.manager import Manager
    from app.models.draft_order import DraftQuantityEvent
    from app.services.draft_order_service import DraftOrderService, DraftOrderError
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from tests.test_email_pipeline import FakeCatalog, email_message
    from dataclasses import replace
    from sqlalchemy import select, func
    factory,engine=staging_sessions
    async def run():
        uid=uuid4().hex
        actor=int(uid[:12],16)
        async with factory() as session,session.begin():
            session.add(Manager(telegram_id=actor,name='SYNTHETIC quantity',is_active=True))
        service=DraftOrderService(matching_service=ProductMatchingService(FakeCatalog()),
            price_service=PriceService({'wholesale':'Цена продажи'}),
            counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])),notifier=AsyncMock())
        for index,(body,confidence) in enumerate((('Chanel Allure Homme Sport x3','confirmed'),('Chanel Allure Homme Sport\n3','probable'),('Chanel Allure Homme Sport','unknown'))):
            message=replace(email_message(uid+str(index),body),sender_email=uid+'@example.invalid')
            draft=await service.ingest_email(message)
            assert draft.items[0].quantity_confidence==confidence
            await service.link_counterparty(draft.id,'synthetic-cp-'+uid,'SYNTHETIC quantity')
            if confidence!='confirmed':
                with pytest.raises(DraftOrderError):await service.finalize(draft.id)
                await service.repository.correct_quantity(draft.id,draft.items[0].id,3,actor)
            order=await service.finalize(draft.id)
            assert order.items[0].qty==3 and order.customer_type=='wholesale'
            assert (await service.ingest_email(message)).id==draft.id
            assert (await service.finalize(draft.id)).id==order.id
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(DraftQuantityEvent).where(DraftQuantityEvent.actor_telegram_id==actor))==2
        await engine.dispose()
    asyncio.run(run())


def test_buying_roles_sessions_and_received_actor_postgres(staging_sessions, monkeypatch):
    from app.api import buying as api
    from app.models.buying import BuyingUser, BuyingSupplier, BuyingPurchase, BuyingEvent
    from app.models.supply import Supplier
    from app.services.buying_passwords import hash_password
    from app.config.settings import settings
    from fastapi import FastAPI
    from tests.asgi_client import request
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    monkeypatch.setattr(settings, 'buying_session_secret', 'synthetic-roles-session-secret-for-postgres')
    api.attempts.clear()
    app = FastAPI()
    for router in (api.auth_router, api.router, api.picker_router): app.include_router(router)
    async def run():
        name='synthetic_'+uuid4().hex
        async with factory() as s,s.begin():
            user=BuyingUser(username=name,role='picker',password_hash=hash_password('synthetic-password-only'))
            supplier=Supplier(name='SYNTHETIC roles',supplier_type='external_wholesaler')
            s.add_all([user,supplier]); await s.flush()
            s.add(BuyingSupplier(supplier_id=supplier.id,currency='RUB',parser={}))
            p=BuyingPurchase(supplier_id=supplier.id,snapshot={'supplier_name':supplier.name,'items':[{'name':'SYNTHETIC','quantity':2,'unit_price_minor':100}]},status='sent',send_state='simulated')
            s.add(p); await s.flush(); pid=p.id; uid=user.id
        status,body,_=await request(app,'POST','/buying/auth/login',{'username':name,'password':'synthetic-password-only'},raise_errors=True)
        assert status==200
        headers={'Authorization':'Bearer '+body['access_token']}
        assert (await request(app,'GET','/buying/catalog',headers=headers))[0]==403
        a,b=await asyncio.gather(*(request(app,'POST',f'/buying/picker/pickups/{pid}/received',headers=headers,raise_errors=True) for _ in range(2)))
        assert a[0]==b[0]==200 and a[1]==b[1]
        assert a[1]['received_by_user_id']==uid and a[1]['received_by_username']==name
        assert 'unit_price_minor' not in str(a[1])
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingEvent).where(BuyingEvent.purchase_id==pid,BuyingEvent.action=='received'))==1
        assert (await request(app,'POST','/buying/auth/logout',headers=headers))[0]==200
        assert (await request(app,'GET','/buying/auth/me',headers=headers))[0]==401
        await engine.dispose()
    asyncio.run(run())


def test_buying_atomic_checkout_and_replay(staging_sessions):
    from app.api import buying as api
    from app.services import buying as svc
    from app.services.buying_excel import ParserConfig, ColumnPriceListParser
    from tests.test_buying import xlsx
    from app.models.buying import BuyingCart, BuyingPurchase, BuyingReply, BuyingEvent
    from app.models.supply import SupplierOffer
    from sqlalchemy import select, delete, func
    from fastapi import HTTPException
    factory, engine = staging_sessions
    async def run():
        uid=uuid4().hex
        async with factory() as s,s.begin():
            await s.execute(delete(BuyingCart))
        suppliers=[]
        for index in range(2):
            result=await api.create_supplier(api.SupplierInput(name='SYNTHETIC Buying '+uid+str(index),email='synthetic@example.invalid',currency='RUB',parser=ParserConfig()))
            suppliers.append(result['id'])
            async with factory() as s,s.begin():
                r=await svc.preview_import(s,result['id'],'synthetic.xlsx',ColumnPriceListParser(ParserConfig()).parse(xlsx([['Name','Price'],['SYNTHETIC '+uid,'10']])) )
                rid=r.id
            async with factory() as s,s.begin():
                await svc.confirm_import(s,result['id'],rid)
        async with factory() as s:
            offers=(await s.scalars(select(SupplierOffer).where(SupplierOffer.supplier_id.in_(suppliers)))).all()
            assert len({o.product_id for o in offers})==1
        for o in offers:
            await api.add_cart(api.CartAdd(offer_id=o.id,quantity=2))
        preview=await api.checkout_preview()
        async def confirm():
            return await api.checkout_confirm(api.CheckoutConfirm(fingerprint=preview['fingerprint']),uid,'synthetic-session')
        a,b=await asyncio.gather(confirm(),confirm())
        assert a==b and len(a['purchase_ids'])==2
        assert (await api.cart())['items']==[]
        pid=a['purchase_ids'][0]
        a,b=await asyncio.gather(api.simulate_send(pid,'synthetic-session'),api.simulate_send(pid,'synthetic-session'))
        assert a['message_id']==b['message_id']
        await engine.dispose()
        assert (await api.purchase_detail(pid))['send_state']=='simulated'
        a,b=await asyncio.gather(api.received(pid,'synthetic-session'),api.received(pid,'synthetic-session'))
        assert a['received_at']==b['received_at']
        async with factory() as s,s.begin():
            args=dict(message_id=uid,thread_id=a['thread_id'],sender='synthetic@example.invalid',received_at=svc.now(),subject='SYNTHETIC reply',body='Any supplier reply')
            await svc.ingest_reply(s,**args)
            await svc.ingest_reply(s,**args)
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingReply).where(BuyingReply.message_id==uid))==1
            assert await s.scalar(select(func.count()).select_from(BuyingEvent).where(BuyingEvent.purchase_id==pid,BuyingEvent.action=='received'))==1
        # Roll back the first row if a later row conflicts with its preview.
        async with factory() as s,s.begin():
            r=await svc.preview_import(s,suppliers[0],'rollback.xlsx',ColumnPriceListParser(ParserConfig()).parse(xlsx([['Name','Price'],['SYNTHETIC '+uid,'20'],['SYNTHETIC second '+uid,'30']])) )
            rid=r.id
        async with factory() as s,s.begin():
            p=await s.get(SupplierOffer,offers[0].id); p.purchase_price_minor=1100
        with pytest.raises(HTTPException):
            async with factory() as s,s.begin():
                await svc.confirm_import(s,suppliers[0],rid)
        assert (await api.purchase_detail(pid))['items'][0]['unit_price_minor']==1000
        await engine.dispose()
    asyncio.run(run())


def test_supply_external_email_and_website_channels(staging_sessions):
    from dataclasses import replace
    from sqlalchemy import select
    from app.models.supply import Supplier, ProductSupply, SupplierOffer, ProcurementRequest
    from app.models.fulfillment import CheckoutRequest
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.services.checkout_service import CheckoutService
    from app.schemas.checkout import CheckoutCreate
    from app.repositories.draft_order_repository import DraftOrderRepository
    from tests.test_email_pipeline import email_message
    from tests.test_readiness_v2 import checkout_payload
    from app.models.manager import Manager
    from app.services.supply_service import ProcurementService
    from app.services.fx import FakeFxProvider
    from app.services.order_operations import OrderOperations, OrderAction
    factory, engine = staging_sessions
    async def run():
        pid = 'external-'+uuid4().hex
        tid = int(uuid4().hex[:12],16)
        async with factory() as session, session.begin():
            session.add(Manager(telegram_id=tid,name='SYNTHETIC external lifecycle',is_active=True))
            supplier = Supplier(name='SYNTHETIC channel supplier',supplier_type='external_wholesaler')
            session.add(supplier);await session.flush()
            session.add(ProductSupply(product_id=pid,source_type='external'));await session.flush()
            offer = SupplierOffer(product_id=pid,supplier_id=supplier.id,purchase_price_minor=4250,currency_code='USD')
            session.add(offer);await session.flush()
        raw = {'id':pid,'name':'Product','salePrices':[{'value':56000,'priceType':{'name':'W'}},{'value':99000,'priceType':{'name':'R'}}]}
        service = DraftOrderService(matching_service=ProductMatchingService(SimpleNamespace(get_products=lambda:[raw])),
            price_service=PriceService({'wholesale':'W','retail':'R'}),
            counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])),notifier=AsyncMock())
        draft = await service.ingest_email(replace(email_message(uuid4().hex,'Product x3'),sender_email=uuid4().hex+'@example.invalid'))
        assert draft.source == 'email' and draft.customer_type == 'wholesale' and draft.items[0].price == 56000
        await service.link_counterparty(draft.id,'synthetic-email-'+pid,'Synthetic')
        email_order = await service.finalize(draft.id)
        assert (await service.finalize(draft.id)).id == email_order.id
        key = uuid4().hex
        catalog = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{'id':pid,'name':'Product','price':99000,
            'supply_source':'external','supply_availability':'on_request','stocks':[]}]))
        payload = CheckoutCreate(**{**checkout_payload(),'email':uuid4().hex+'@example.invalid','items':[{'product_id':pid,'qty':2}]})
        await CheckoutService(catalog).submit(payload,key)
        async with factory() as session:
            checkout = await session.get(CheckoutRequest,key)
        await service.link_counterparty(checkout.draft_id,'synthetic-web-'+pid,'Synthetic')
        web_order = await service.finalize(checkout.draft_id)
        assert web_order.source == 'website' and web_order.customer_type == 'retail' and web_order.items[0].price == 99000
        async with factory() as session:
            requests = list((await session.scalars(select(ProcurementRequest).where(ProcurementRequest.order_id.in_([email_order.id,web_order.id])))).all())
            assert len(requests) == 2 and all(r.status == 'needed' and r.offer_id is None for r in requests)
        procurement = ProcurementService(FakeFxProvider('10'))
        await procurement.refresh_estimate(offer.id)
        email_request = next(r for r in requests if r.order_id == email_order.id)
        for revision, action in enumerate(('select','requested','confirmed','received')):
            await procurement.act(email_request.id,tid,action,revision,offer_id=offer.id if action == 'select' else None)
        for revision, action in enumerate(('assembling','assembled','delivery','shipped','paid')):
            updated = await OrderOperations().act(email_order.id,tid,OrderAction(action=action,expected_revision=revision,
                idempotency_key=pid+action,**({'delivery_method':'pickup'} if action=='delivery' else {})))
        assert updated.fulfillment_status == 'shipped' and updated.payment_status == 'paid'
        await OrderOperations().act(web_order.id,tid,OrderAction(action='cancel',expected_revision=0,idempotency_key=pid+'cancel'))
        async with factory() as session:
            web_request = await session.scalar(select(ProcurementRequest).where(ProcurementRequest.order_id==web_order.id))
            assert web_request.status == 'cancelled'
        await engine.dispose()
    asyncio.run(run())


def test_supply_procurement_vertical_slice_concurrency_and_immutable_cost(staging_sessions, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from sqlalchemy import select, func, update
    from sqlalchemy.exc import DBAPIError
    from app.models.manager import Manager
    from app.models.order import OrderItem
    from app.models.supply import Supplier, ProductSupply, SupplierOffer, OrderItemSupply, XSettlement, ProcurementRequest, SupplyEvent
    from app.repositories.order_repository import create_order
    from app.services.supply_service import ProcurementService, SupplyError, catalog_supply
    from app.services.fx import FakeFxProvider
    from app.bot.procurement import supply_order_card
    from app.api.supply import source_set, SourceInput
    from fastapi import HTTPException
    factory, engine = staging_sessions
    async def run():
        uid, tid = uuid4().hex, int(uuid4().hex[:12],16)
        own, x, external = ['supply-'+uid+'-'+v for v in ('own','x','external')]
        async with factory() as session, session.begin():
            session.add(Manager(telegram_id=tid, name='SYNTHETIC supply manager', is_active=True))
            suppliers = [Supplier(name='SYNTHETIC X '+uid,supplier_type='partner_x'),
                Supplier(name='SYNTHETIC A '+uid,supplier_type='external_wholesaler'),
                Supplier(name='SYNTHETIC B '+uid,supplier_type='external_wholesaler')]
            session.add_all(suppliers)
            await session.flush()
            session.add_all([ProductSupply(product_id=own,source_type='own'),
                ProductSupply(product_id=x,source_type='partner_x',supplier_id=suppliers[0].id,base_cost_minor=200000),
                ProductSupply(product_id=external,source_type='external')])
            await session.flush()
            offers = [SupplierOffer(product_id=external,supplier_id=suppliers[n].id,purchase_price_minor=4200+n*50,currency_code='USD',availability='confirmed',availability_qty=10) for n in (1,2)]
            session.add_all(offers)
            await session.flush()
        with pytest.raises(HTTPException) as conflict:
            await source_set(x,SourceInput(source_type='own'))
        assert conflict.value.status_code == 409
        service = ProcurementService(FakeFxProvider('90', date(2026,9,15)))
        estimate = await service.refresh_estimate(offers[1].id)
        assert estimate.estimated_purchase_cost_rub_minor == 387000
        order = await create_order(dict(customer_name='SYNTHETIC supply '+uid,phone='',customer_type='wholesale',source='email',
            total=1400001,items=[dict(id=own,name='SYNTHETIC Own',price=100000,qty=1,sum=100000),
                dict(id=x,name='SYNTHETIC X',price=300001,qty=1,sum=300001),
                dict(id=external,name='SYNTHETIC External USD',price=500000,qty=2,sum=1000000)]))
        async with factory() as session:
            rows = list((await session.scalars(select(OrderItemSupply).where(OrderItemSupply.order_id==order.id))).all())
            assert {r.source_type for r in rows} == {'own','partner_x','external'}
            settlement = await session.scalar(select(XSettlement).where(XSettlement.order_id==order.id))
            assert (settlement.partner_margin_minor,settlement.our_margin_minor,settlement.partner_due_minor) == (50000,50001,250000)
            request = await session.scalar(select(ProcurementRequest).where(ProcurementRequest.order_id==order.id))
            assert request.offer_id is None  # no cheapest-supplier policy
        with pytest.raises(SupplyError):
            await service.act(request.id,tid+1,'select',0,offer_id=offers[1].id)
        with pytest.raises(SupplyError):
            await service.act(request.id,tid,'confirmed',0)
        selected = await asyncio.gather(*(service.act(request.id,tid,'select',0,offer_id=offers[1].id) for _ in range(4)))
        assert all(r.revision == 1 for r in selected)
        with pytest.raises(SupplyError):
            await service.act(request.id,tid,'select',0,offer_id=offers[0].id)
        await service.act(request.id,tid,'fx',1,manual_rate='91.25')
        await service.act(request.id,tid,'requested',2)
        await asyncio.gather(*(service.act(request.id,tid,'confirmed',3) for _ in range(3)))
        async with factory() as session:
            frozen = (await session.get(OrderItemSupply,request.order_item_id)).cost_snapshot
            assert frozen['original_purchase_price_minor'] == 4300
            assert frozen['converted_purchase_cost_rub_minor'] == 784750
            assert frozen['margin_rub_minor'] == 215250
            assert frozen['fx_source'] == 'manual' and frozen['fx_manager_id'] is not None
            assert await session.scalar(select(func.count()).select_from(ProcurementRequest).where(ProcurementRequest.order_item_id==request.order_item_id)) == 1
            assert await session.scalar(select(func.count()).select_from(SupplyEvent).where(SupplyEvent.procurement_id==request.id)) == 4
        async with factory() as session, session.begin():
            offer = await session.get(SupplierOffer, offers[1].id)
            offer.purchase_price_minor = 5000
            (await session.get(ProductSupply,x)).base_cost_minor = 220000
        await ProcurementService(FakeFxProvider('100')).refresh_estimate(offers[1].id)
        await engine.dispose()  # worker restart: re-open connections, read committed snapshot
        async with factory() as session:
            assert (await session.get(OrderItemSupply,request.order_item_id)).cost_snapshot == frozen
            assert (await session.get(XSettlement,settlement.id)).base_cost_minor == 200000
        with pytest.raises(SupplyError):
            await service.act(request.id,tid,'fx',4,manual_rate='99')
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(update(OrderItemSupply).where(OrderItemSupply.order_item_id==request.order_item_id).values(cost_snapshot={'changed':True}))
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(update(XSettlement).where(XSettlement.id==settlement.id).values(partner_due_minor=1))
        received = await service.act(request.id,tid,'received',4)
        assert received.status == 'received'
        card = await supply_order_card(order)
        assert all(label in card for label in ('Наш склад','X-склад','Внешние поставщики','к выплате X','Наша маржа','USD'))
        negative = await create_order(dict(customer_name='SYNTHETIC negative '+uid,phone='',customer_type='retail',source='website',total=1,
            items=[dict(id=x,name='SYNTHETIC below cost',price=1,qty=1,sum=1)]))
        assert negative.needs_review
        async with factory() as session:
            review = await session.scalar(select(XSettlement).where(XSettlement.order_id==negative.id))
            assert review.status == 'requires_financial_review' and review.partner_due_minor is None
        report = {'scenario':'supply-procurement','order_id':order.id,'procurement_id':request.id,
            'x_partner_due_minor':250000,'x_our_margin_minor':50001,'fixed_external_cost_minor':784750,
            'external_margin_minor':215250,'concurrency':'passed','restart':'passed','db_immutability':'passed'}
        Path('.staging-artifacts/supply-e2e.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        await engine.dispose()
    asyncio.run(run())


@pytest.fixture
def staging_sessions(monkeypatch):
    if os.getenv("OMS_STAGING_TESTS") != "1":
        pytest.skip("Explicit isolated staging test run required")
    from app.bot.staging_runner import validate_staging_config
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config.settings import settings
    config = validate_staging_config(dict(os.environ))
    schema = os.environ.get("OMS_STAGING_SCHEMA", "")
    import re
    assert re.fullmatch(r"oms_audit_[a-f0-9]{32}", schema), "Only isolated audit schemas allowed"
    url = make_url(config.database_url)
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    connect_args = {"server_settings": {"search_path": schema, "statement_timeout": "30000"}, "timeout": 10, "command_timeout": 30}
    if sslmode:
        connect_args["ssl"] = sslmode != "disable"
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg", query=query),
                                 pool_size=5, max_overflow=5, connect_args=connect_args)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in ("app.repositories.customer_repository", "app.repositories.draft_order_repository",
                   "app.repositories.order_repository", "app.repositories.email_cursor_repository",
                   "app.services.checkout_service", "app.services.fulfillment_service",
                   "app.services.external_operation_service", "app.workers.notifications",
                   "app.services.order_operations", "app.services.client_channel", "app.services.manager_workspace", "app.services.supply_service", "app.bot.procurement", "app.api.supply", "app.api.buying", "app.workers.supplier_replies", "app.workers.buying_notifications"):
        monkeypatch.setattr(importlib.import_module(module), "async_session", factory)
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.setattr(settings, "warehouse_ids", ())
    return factory, engine


async def ready_draft():
    from datetime import datetime, timezone
    from app.repositories.customer_repository import CustomerRepository
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.models.sales import CustomerType
    uid = uuid4().hex
    customer = await CustomerRepository().create(CustomerType.WHOLESALE, "STAGING AUDIT " + uid, [])
    return await DraftOrderRepository().create({"source": "manual", "external_message_id": "audit:" + uid,
        "sender_email": uid + "@example.invalid", "body_text": "Product x4",
        "received_at": datetime.now(timezone.utc), "customer_id": customer.id,
        "customer_type": "wholesale", "counterparty_id": "cp-" + uid,
        "status": "ready", "total": 39996, "items": [{"raw_product_text": "Product",
        "qty": 4, "match_status": "matched", "product_id": "p1", "product_name": "Product",
        "article": "P1", "price": 9999, "item_total": 39996, "candidates": []}]})


def test_concurrent_finalize_creates_one_order(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from sqlalchemy import select, func
    from app.models.order import Order
    factory, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        orders = await asyncio.gather(*(DraftOrderRepository().finalize(draft.id) for _ in range(8)))
        assert len({o.id for o in orders}) == 1
        assert all(o.items[0].price == 9999 and o.total == 39996 and o.moysklad_order_id is None for o in orders)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Order).where(Order.customer_id == draft.customer_id)) == 1
        await engine.dispose()
    asyncio.run(run())


def test_stale_review_and_edit_after_finalize_blocked(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.order_lifecycle import InvalidOrderTransitionError
    _, engine = staging_sessions
    async def run():
        repository = DraftOrderRepository()
        draft = await ready_draft()
        await repository.set_customer_type(draft.id, "retail")
        repository.expected_revision = 0
        with pytest.raises(InvalidOrderTransitionError):
            await repository.set_customer_type(draft.id, "wholesale")
        repository.expected_revision = None
        with pytest.raises(InvalidOrderTransitionError):
            await repository.save_review(draft.id, "ready", 39996, [],
                {draft.items[0].id: (9999, 39996)}, expected_revision=0)
        with pytest.raises(InvalidOrderTransitionError):
            await repository.finalize(draft.id)
        other = await ready_draft()
        await repository.finalize(other.id)
        with pytest.raises(InvalidOrderTransitionError):
            await repository.resolve_item(other.id, other.items[0].id, {"id": "different"})
        await engine.dispose()
    asyncio.run(run())


def test_concurrent_duplicate_email_is_idempotent(staging_sessions):
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.models.sales import CustomerType
    from tests.test_email_pipeline import email_message
    _, engine = staging_sessions
    async def run():
        products = SimpleNamespace(get_products=lambda: [{"id": "p1", "name": "Product",
            "salePrices": [{"value": 9999, "priceType": {"name": "W"}}]}])
        def service():
            return DraftOrderService(matching_service=ProductMatchingService(products),
                price_service=PriceService({CustomerType.WHOLESALE: "W"}),
                counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])), notifier=AsyncMock())
        message = email_message(uuid4().hex, "Product x2")
        from dataclasses import replace
        message = replace(message, sender_email=uuid4().hex + "@example.invalid")
        drafts = await asyncio.gather(*(service().ingest_email(message) for _ in range(5)))
        assert len({d.id for d in drafts}) == 1
        assert drafts[0].customer_type == "wholesale"
        assert drafts[0].items[0].price == 9999
        await engine.dispose()
    asyncio.run(run())


def test_email_channel_conflict_review_and_legacy_reprice(staging_sessions):
    from app.models.customer import Customer, CustomerIdentity
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.services.order_lifecycle import InvalidOrderTransitionError
    from tests.test_email_pipeline import email_message
    from dataclasses import replace
    factory, engine = staging_sessions
    async def run():
        email = uuid4().hex + "@example.invalid"
        async with factory() as session, session.begin():
            customer = Customer(customer_type="retail", display_name="Synthetic conflict")
            customer.identities.append(CustomerIdentity(identity_type="email", normalized_value=email, original_value=email))
            session.add(customer)
        service = DraftOrderService(matching_service=ProductMatchingService(SimpleNamespace(get_products=lambda: [
            {"id": "p1", "name": "Product", "salePrices": [{"value": 56000, "priceType": {"name": "W"}}]}])),
            price_service=PriceService({"wholesale": "W"}),
            counterparty_service=SimpleNamespace(candidates_async=AsyncMock(return_value=[])), notifier=AsyncMock())
        draft = await service.ingest_email(replace(email_message(uuid4().hex, "Product x3"), sender_email=email))
        assert draft.customer_type == "wholesale" and draft.items[0].price == 56000
        assert draft.items[0].item_total == 168000
        assert draft.contact_details["customer_type_policy"]["profile_conflict"]
        repository = DraftOrderRepository()
        with pytest.raises(InvalidOrderTransitionError):
            await repository.set_customer_type(draft.id, "retail")
        reviewed = await service.review(draft.id)
        assert reviewed.contact_details["customer_type_policy"]["profile_conflict"]
        # Represent a pre-policy row, only in this isolated test schema.
        from app.models.draft_order import DraftOrder
        async with factory() as session, session.begin():
            legacy = await session.get(DraftOrder, draft.id)
            legacy.customer_type = "unknown"
        restored = await service.review(draft.id)
        assert restored.customer_type == "wholesale" and restored.items[0].price == 56000
        assert restored.revision == draft.revision + 1
        async with factory() as session:
            assert (await session.get(Customer, customer.id)).customer_type == "retail"
        service.notifier.assert_awaited_once()
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("retail_price", [None, 99000])
def test_website_channel_keeps_retail_for_wholesale_profile(staging_sessions, retail_price):
    from app.models.customer import Customer, CustomerIdentity
    from app.models.draft_order import DraftOrder
    from app.services.checkout_service import CheckoutService
    from app.schemas.checkout import CheckoutCreate
    from tests.test_readiness_v2 import checkout_payload
    from sqlalchemy import select
    factory, engine = staging_sessions
    async def run():
        email = uuid4().hex + "@example.invalid"
        async with factory() as session, session.begin():
            customer = Customer(customer_type="wholesale", display_name="Synthetic website conflict")
            customer.identities.append(CustomerIdentity(identity_type="email", normalized_value=email, original_value=email))
            session.add(customer)
        catalog = AsyncMock(return_value=[{"id": "p1", "name": "Product", "price": retail_price,
            "stocks": [{"id": "w1", "stock": 100, "reserve": 0}]}])
        payload = CheckoutCreate(**{**checkout_payload(), "email": email})
        await CheckoutService(SimpleNamespace(get_catalog_async=catalog)).submit(payload, uuid4().hex)
        catalog.assert_awaited_once_with(customer_type="retail")
        async with factory() as session:
            draft = (await session.execute(select(DraftOrder).where(DraftOrder.customer_id == customer.id))).scalar_one()
            assert draft.customer_type == "retail"
            assert draft.contact_details["customer_type_policy"]["profile_conflict"]
            assert (draft.total is None) == (retail_price is None)
            assert (await session.get(Customer, customer.id)).customer_type == "wholesale"
        await engine.dispose()
    asyncio.run(run())


def test_checkout_concurrency_retail_review_and_conflict(staging_sessions):
    from app.services.checkout_service import CheckoutService, CheckoutConflict
    from app.schemas.checkout import CheckoutCreate
    from app.models.fulfillment import CheckoutRequest
    from sqlalchemy import select
    from tests.test_readiness_v2 import checkout_payload
    factory, engine = staging_sessions
    async def run():
        products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{"id": "p1", "name": "Product",
            "price": None, "stocks": [{"id": "w1", "stock": 10, "reserve": 0}]}]))
        payload = CheckoutCreate(**{**checkout_payload(), "email": uuid4().hex + "@example.invalid"})
        key = uuid4().hex
        service = CheckoutService(products)
        responses = await asyncio.gather(*(service.submit(payload, key) for _ in range(5)))
        assert all(r == responses[0] and r["total_minor"] is None for r in responses)
        changed = payload.model_copy(update={"comment": "changed"})
        with pytest.raises(CheckoutConflict):
            await service.submit(changed, key)
        async with factory() as session:
            assert len((await session.execute(select(CheckoutRequest).where(CheckoutRequest.key == key))).scalars().all()) == 1
        await engine.dispose()
    asyncio.run(run())


def test_warehouse_split_persisted_once(staging_sessions):
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.fulfillment_service import FulfillmentService
    _, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        order = await DraftOrderRepository().finalize(draft.id)
        service = FulfillmentService(SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{
            "id": "p1", "stocks": [{"id": "w1", "stock": 3, "reserve": 1}, {"id": "w2", "stock": 4, "reserve": 1}]}])))
        plans = await asyncio.gather(service.plan(order.id), service.plan(order.id))
        assert {s.id for s in plans[0]} == {s.id for s in plans[1]}
        assert len(plans[0]) == 2
        assert sum(a.qty for s in plans[0] for a in s.allocations) == 4
        await engine.dispose()
    asyncio.run(run())


def test_phase3_review_round_trips_and_duration(staging_sessions):
    from sqlalchemy import event
    from app.services.draft_order_service import DraftOrderService
    from app.services.product_matching_service import ProductMatchingService
    from app.services.price_service import PriceService
    from app.models.sales import CustomerType
    _, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        service = DraftOrderService(matching_service=ProductMatchingService(SimpleNamespace(get_products=lambda: [
            {"id": "p1", "name": "Product", "salePrices": [{"value": 9999, "priceType": {"name": "W"}}]}])),
            price_service=PriceService({CustomerType.WHOLESALE: "W"}), notifier=AsyncMock())
        queries = []
        def record(*args):
            queries.append(args[2].split()[0])
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        start = perf_counter()
        result = await service.set_customer_type(draft.id, CustomerType.WHOLESALE)
        duration = (perf_counter() - start) * 1000
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert result.status == "ready" and result.total == 39996
        assert queries.count("SELECT") <= 8
        Path(".staging-artifacts/performance.json").write_text(json.dumps({
            "operation": "set_customer_type", "duration_ms": round(duration, 2),
            "sql_statements": len(queries), "selects": queries.count("SELECT"),
            "network": "Railway staging from local workstation", "moysklad": "fake catalog"}), encoding="utf-8")
        await engine.dispose()
    asyncio.run(run())


def test_manager_lifecycle_atomic_replay_audit_and_permissions(staging_sessions):
    from app.models.manager import Manager
    from app.models.operations import OrderEvent
    from app.models.order import Order
    from app.services.order_operations import OrderOperations, OrderAction, OperationError, ManagerDenied
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.fulfillment_service import FulfillmentService
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        async with factory() as session, session.begin():
            manager = Manager(telegram_id=tid, name="RC test", is_active=True)
            session.add(manager)
        draft = await ready_draft()
        order = await DraftOrderRepository().finalize(draft.id)
        service = OrderOperations()
        def req(action, version, **kw):
            return OrderAction(action=action, expected_revision=version, idempotency_key=f"{action}:{version}", **kw)
        with pytest.raises(ManagerDenied):
            await service.act(order.id, tid+1, req("paid", 0))
        with pytest.raises(OperationError):
            await service.act(order.id, tid, req("assembling", 0))
        products = SimpleNamespace(get_catalog_async=AsyncMock(return_value=[{"id": "p1", "stocks": [{"id": "w1", "stock": 2}, {"id": "w2", "stock": 2}]}]))
        await FulfillmentService(products).plan(order.id)
        expected = [("delivery", {"delivery_method": "manual"}), ("assembling", {}), ("assembled", {}), ("shipped", {}), ("paid", {"note": "RC verification"})]
        for version, (action, kwargs) in enumerate(expected):
            request = req(action, version, **kwargs)
            results = await asyncio.gather(*(service.act(order.id, tid, request) for _ in range(4)))
            assert all(o.revision == version+1 for o in results)
            async with factory() as session:
                persisted = await session.get(Order, order.id)
                assert persisted.revision == version+1 and persisted.total == 39996
                assert await session.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order.id)) == version+1
        assert results[0].paid_at and results[0].shipped_at and results[0].assembled_at
        assert results[0].paid_by_manager_id == manager.id
        assert results[0].moysklad_order_id is None
        with pytest.raises(OperationError):
            await service.act(order.id, tid, req("review", 0))
        # One of two different concurrent actions based on the same revision wins.
        actions = [req("review", 5), req("delivery", 5, delivery_status="delivered")]
        results = await asyncio.gather(*(service.act(order.id, tid, r) for r in actions), return_exceptions=True)
        assert sum(isinstance(r, OperationError) for r in results) == 1
        await engine.dispose()
    asyncio.run(run())


def test_client_conversation_replay_ownership_existing_identity_and_restart(staging_sessions):
    from app.services.client_channel import ClientChannel
    from app.models.customer import Customer, CustomerIdentity
    from app.models.draft_order import DraftOrder
    from app.models.notification import DraftNotification
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        phone = "+7" + str(tid)[-10:].zfill(10)
        async with factory() as session, session.begin():
            customer = Customer(customer_type="wholesale", display_name="Existing RC customer")
            customer.identities.append(CustomerIdentity(identity_type="phone", normalized_value=phone, original_value=phone))
            session.add(customer)
        await ClientChannel().handle(tid, 1, "/start")
        await ClientChannel().handle(tid, 2, "/request")
        await ClientChannel().handle(tid, 3, "Свеча; 2\nАромат; 1")
        await ClientChannel().handle(tid, 4, "Контакт", verified_phone=phone)
        responses = await asyncio.gather(*(ClientChannel().handle(tid, 5, "/send") for _ in range(5)))
        assert len(set(responses)) == 1
        import re
        draft_id = int(re.search(r"№(\d+)", responses[0]).group(1))
        async with factory() as session:
            draft = await session.get(DraftOrder, draft_id)
            assert draft.customer_id == customer.id and draft.customer_type == "wholesale"
            assert draft.source == "telegram" and draft.total is None and draft.status == "needs_review"
            assert await session.scalar(select(func.count()).select_from(DraftOrder).where(DraftOrder.customer_id == customer.id)) == 1
            assert await session.scalar(select(func.count()).select_from(DraftNotification).where(DraftNotification.draft_id == draft_id)) == 1
            identity = (await session.execute(select(CustomerIdentity).where(CustomerIdentity.identity_type == "telegram", CustomerIdentity.normalized_value == str(tid)))).scalar_one()
            assert identity.customer_id == customer.id
        assert "менеджер проверяет" in await ClientChannel().handle(tid, 6, f"/status {draft_id}")
        assert "не найдена" in await ClientChannel().handle(tid+1, 1, f"/status {draft_id}")
        assert "передана просьба" in await ClientChannel().handle(tid, 7, "/manager")
        await engine.dispose()
    asyncio.run(run())


def test_direct_order_concurrent_idempotency(staging_sessions):
    from app.repositories.order_repository import create_order
    from app.services.checkout_service import CheckoutConflict
    _, engine = staging_sessions
    async def run():
        data = {"_request_key": uuid4().hex, "_request_hash": "a"*64,
            "customer_name": "RC", "phone": "", "customer_type": "retail", "source": "manual", "total": 100,
            "items": [{"id": "p", "name": "Product", "price": 100, "qty": 1, "sum": 100}]}
        results = await asyncio.gather(*(create_order(data) for _ in range(5)))
        assert len({o.id for o in results}) == 1
        with pytest.raises(CheckoutConflict):
            await create_order({**data, "_request_hash": "b"*64})
        await engine.dispose()
    asyncio.run(run())


def test_poller_session_lock_has_one_owner(staging_sessions):
    from sqlalchemy import text
    _, engine = staging_sessions
    async def run():
        key = int(uuid4().hex[:12], 16)
        async with engine.connect() as first, engine.connect() as second:
            assert await first.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            assert not await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            assert await second.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            await second.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        await engine.dispose()
    asyncio.run(run())


def test_unverified_client_contact_routes_existing_profile_without_identity_takeover(staging_sessions):
    from app.models.customer import Customer, CustomerIdentity
    from app.models.draft_order import DraftOrder
    from app.services.client_channel import ClientChannel
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        email = uuid4().hex + "@example.invalid"
        async with factory() as session, session.begin():
            customer = Customer(customer_type="wholesale", display_name="Existing")
            customer.identities.append(CustomerIdentity(identity_type="email", normalized_value=email, original_value=email))
            session.add(customer)
        for mid, message in enumerate(("/request", "Product; 1", email, "/send"), 1):
            response = await ClientChannel().handle(tid, mid, message)
        assert "Заявка №" in response
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(DraftOrder).where(DraftOrder.customer_id == customer.id)) == 1
            assert await session.scalar(select(func.count()).select_from(CustomerIdentity).where(CustomerIdentity.identity_type == "telegram", CustomerIdentity.normalized_value == str(tid))) == 0
        await engine.dispose()
    asyncio.run(run())


def test_finalize_crash_rolls_back_then_retry_once(staging_sessions, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select, func
    from app.models.order import Order
    from app.repositories.draft_order_repository import DraftOrderRepository
    factory, engine = staging_sessions
    async def run():
        draft = await ready_draft()
        original = AsyncSession.commit
        async def crash(self):
            raise asyncio.CancelledError()
        monkeypatch.setattr(AsyncSession, "commit", crash)
        with pytest.raises(asyncio.CancelledError):
            await DraftOrderRepository().finalize(draft.id)
        monkeypatch.setattr(AsyncSession, "commit", original)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Order).where(Order.customer_id == draft.customer_id)) == 0
        first = await DraftOrderRepository().finalize(draft.id)
        await engine.dispose()  # Simulated process connection teardown, persisted state retained.
        second = await DraftOrderRepository().finalize(draft.id)
        assert first.id == second.id
        await engine.dispose()
    asyncio.run(run())


def test_two_managers_pay_once_and_concurrent_match_rejects_stale(staging_sessions):
    from app.models.manager import Manager
    from app.models.operations import OrderEvent
    from app.services.order_operations import OrderOperations, OrderAction, OperationError
    from app.repositories.draft_order_repository import DraftOrderRepository
    from app.services.order_lifecycle import InvalidOrderTransitionError
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        async with factory() as session, session.begin():
            session.add_all([Manager(telegram_id=tid+i, name="SYNTHETIC two managers", is_active=True) for i in range(2)])
        draft = await ready_draft()
        order = await DraftOrderRepository().finalize(draft.id)
        results = await asyncio.gather(*(OrderOperations().act(order.id, tid+i,
            OrderAction(action="paid", expected_revision=0, idempotency_key=f"manager:{tid+i}")) for i in range(2)), return_exceptions=True)
        assert sum(isinstance(r, OperationError) for r in results) == 1
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order.id)) == 1
        other = await ready_draft()
        repositories = [DraftOrderRepository(), DraftOrderRepository()]
        for repo in repositories: repo.expected_revision = 0
        results = await asyncio.gather(*(repo.set_item_candidates(other.id, other.items[0].id, []) for repo in repositories), return_exceptions=True)
        assert sum(isinstance(r, InvalidOrderTransitionError) for r in results) == 1
        await engine.dispose()
    asyncio.run(run())


def test_client_transport_restart_email_and_double_submit(staging_sessions):
    from aiogram import Bot
    from aiogram.types import Update
    from tests.telegram_transport import TelegramSession
    from app.bot.client_bot import create_dispatcher
    from app.models.draft_order import DraftOrder
    from sqlalchemy import select, func
    factory, engine = staging_sessions
    async def run():
        tid = int(uuid4().hex[:12], 16)
        transport = TelegramSession(); bot = Bot("123456:FAKE_LOCAL_TEST_TOKEN", session=transport)
        email = f"synthetic-{tid}@example.invalid"
        async def send(number, value):
            update = Update.model_validate({"update_id": number, "message": {"message_id": number, "date": 1,
                "chat": {"id": tid, "type": "private"}, "from": {"id": tid, "is_bot": False, "first_name": "SYNTHETIC"}, "text": value}})
            await create_dispatcher().feed_update(bot, update)  # New dispatcher/service each update.
        for number, value in enumerate(["/start", "/request", "Свеча; 2", email], 1):
            await send(number, value)
        await engine.dispose()
        await asyncio.gather(send(5, "/send"), send(6, "/send"), send(5, "/send"))
        async with factory() as session:
            drafts = list((await session.execute(select(DraftOrder).where(DraftOrder.sender_email == email))).scalars())
            assert len(drafts) == 1 and drafts[0].contact_details["email"] == email
            draft_id = drafts[0].id
        await send(7, f"/status {draft_id}")
        await send(8, "/manager")
        assert "передана просьба" in transport.calls[-1].text
        await bot.session.close(); await engine.dispose()
    asyncio.run(run())
