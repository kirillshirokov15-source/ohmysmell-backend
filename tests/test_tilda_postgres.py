"""Real PostgreSQL locks/uniqueness plus real aiogram dispatch with fake transport."""
import asyncio
import hashlib
import importlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from tests.test_postgres_staging import staging_sessions
from tests.test_tilda_orders import tilda_config, payload
from app.integrations.tilda import normalize
from app.services.order_desk import TildaIntake, DeskService, DeskError, now
from app.models.order_desk import OrderDesk, OrderLink, DeskEvent, DeskMessage
from app.models.draft_order import DraftOrder
from app.models.manager import Manager
from app.models.order import Order

pytestmark = pytest.mark.staging


@pytest.fixture
def desk_db(staging_sessions, tilda_config, monkeypatch):
    factory, engine = staging_sessions
    # Baseline fake transport tests exercise unrestricted delivery; staging safety
    # tests below explicitly override this and configure audited scopes.
    from app.config.settings import settings
    monkeypatch.setattr(settings, "environment", "development")
    for module in ("app.services.order_desk", "app.services.desk_display", "app.workers.desk_outbox", "app.database.session", "app.repositories.manager_repository"):
        monkeypatch.setattr(importlib.import_module(module), "async_session", factory)
    # IDs are unique per test, and never use any real manager identity.
    actors = (int(uuid4().hex[:10],16), int(uuid4().hex[:10],16))
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", ",".join(map(str,actors)))
    return factory, engine, actors


async def seed(factory, actors):
    async with factory() as session, session.begin():
        session.add_all([Manager(telegram_id=a,name=f"SYNTHETIC manager {i}",is_active=True) for i,a in enumerate(actors)])
    return (await TildaIntake().submit(normalize(payload(uuid4().hex))))["draft_id"]


def test_concurrent_duplicate_and_db_uniqueness(desk_db):
    factory, engine, actors = desk_db
    async def run():
        data=normalize(payload(uuid4().hex))
        results=await asyncio.gather(*(TildaIntake().submit(data) for _ in range(8)))
        assert len({r["draft_id"] for r in results})==1
        assert sum(not r["duplicate"] for r in results)==1
        did=results[0]["draft_id"]
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(DeskMessage).where(DeskMessage.draft_id==did))==1
            draft=await s.get(DraftOrder,did)
            assert draft.customer_type=="retail" and draft.total==3030 and draft.finalized_order_id is None
        altered={**data,"comment":"different"}
        with pytest.raises(DeskError): await TildaIntake().submit(altered)
        from sqlalchemy.exc import IntegrityError
        async with factory() as s:
            # A different PK isolates the external identity UNIQUE constraint.
            other=(await TildaIntake().submit(normalize(payload(uuid4().hex))))["draft_id"]
            from sqlalchemy import update
            with pytest.raises(IntegrityError):
                await s.execute(update(OrderDesk).where(OrderDesk.draft_id==other).values(external_id=data["external_id"]))
        await engine.dispose()
    asyncio.run(run())


def test_claim_concurrent_one_winner_and_actor_audit(desk_db):
    factory, engine, actors=desk_db
    async def run():
        did=await seed(factory,actors)
        a,b=await asyncio.gather(*(DeskService().claim(did,actor,-100123456) for actor in actors))
        assert a.actor_telegram_id==b.actor_telegram_id
        winner=a.actor_telegram_id
        assert (await DeskService().claim(did,winner,-100123456)).actor_telegram_id==winner
        async with factory() as s:
            events=(await s.scalars(select(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=="manager_claim_succeeded"))).all()
            assert len(events)==1 and events[0].actor_telegram_id==winner
        from app.services.desk_display import desk_card
        card,_=await desk_card(did)
        assert "Ответственный: SYNTHETIC manager" in card
        for actor,chat in [(333,-100123456),(winner,-999),(winner,winner)]:
            with pytest.raises(DeskError): await DeskService().claim(did,actor,chat)
        async with factory() as s,s.begin():
            manager=await s.scalar(select(Manager).where(Manager.telegram_id==winner));manager.is_active=False
        with pytest.raises(DeskError):await DeskService().claim(did,winner,-100123456)
        await engine.dispose()
    asyncio.run(run())


def test_links_expiry_reuse_ownership_and_multiple_orders(desk_db):
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors); service=DeskService()
        token=(await service.issue_link(did))["deep_link"].split("start=")[1]
        assert len(token)==43
        async with factory() as s:
            link=await s.get(OrderLink,hashlib.sha256(token.encode()).hexdigest())
            assert link and token not in str(link.__dict__)
        await service.client(9001,1,"/start "+token)
        await service.client(9001,1,"/start "+token)
        await service.client(9001,2,"/start "+token)
        await service.client(9002,1,"/start "+token)
        await service.client(9002,2,f"/status {did}")
        async with factory() as s:
            assert (await s.get(OrderDesk,did)).customer_telegram_id==9001
            bad=(await s.scalars(select(DeskMessage).where(DeskMessage.destination==9002))).all()
            assert all("SYNTHETIC" not in m.body for m in bad)
            assert await s.scalar(select(func.count()).select_from(DeskMessage).where(DeskMessage.idempotency_key=="desk:client:9001:1:ack"))==1
        second=(await TildaIntake().submit(normalize(payload(uuid4().hex))))["draft_id"]
        token2=(await service.issue_link(second))["deep_link"].split("start=")[1]
        await service.client(9001,3,"/start "+token2)
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(OrderDesk).where(OrderDesk.customer_telegram_id==9001))==2
        third=(await TildaIntake().submit(normalize(payload(uuid4().hex))))["draft_id"]
        expired=(await service.issue_link(third))["deep_link"].split("start=")[1]
        async with factory() as s,s.begin():
            link=await s.get(OrderLink,hashlib.sha256(expired.encode()).hexdigest());link.expires_at=now()-timedelta(seconds=1)
        await service.client(9003,1,"/start "+expired)
        await service.client(9003,2,"/start invalid")
        async with factory() as s: assert (await s.get(OrderDesk,third)).customer_telegram_id is None
        await engine.dispose()
    asyncio.run(run())


def test_retail_profile_total_review_and_missing_mapping(desk_db, monkeypatch):
    from app.models.customer import Customer,CustomerIdentity
    factory,engine,actors=desk_db
    async def run():
        email=uuid4().hex+"@example.invalid"
        async with factory() as s,s.begin():
            customer=Customer(customer_type="wholesale",display_name="SYNTHETIC")
            customer.identities.append(CustomerIdentity(identity_type="email",normalized_value=email,original_value=email))
            s.add(customer);await s.flush();cid=customer.id
        data=payload(uuid4().hex);data["Email"]=email;data["amount"]="1.00"
        did=(await TildaIntake().submit(normalize(data)))["draft_id"]
        async with factory() as s:
            draft=await s.get(DraftOrder,did)
            assert draft.customer_type=="retail" and draft.customer_id==cid and draft.total is None
            assert (await s.get(Customer,cid)).customer_type=="wholesale"
        monkeypatch.setenv("TILDA_PRODUCT_MAP_JSON","{}")
        missing=(await TildaIntake().submit(normalize(payload(uuid4().hex))))["draft_id"]
        async with factory() as s:
            assert (await s.get(DraftOrder,missing)).total is None
        await engine.dispose()
    asyncio.run(run())


def test_fake_tilda_to_manager_client_helpdesk_and_manual_lifecycle(desk_db,monkeypatch):
    from aiogram import Bot
    from aiogram.types import Update
    from tests.telegram_transport import TelegramSession
    from app.bot.client_bot import create_dispatcher
    from app.bot.telegram_bot import dp
    from app.api.tilda import router
    from fastapi import FastAPI
    from tests.asgi_client import request
    from app.workers.desk_outbox import run_once
    from app.services.order_operations import OrderOperations,OrderAction,ManagerDenied
    from app.models.fulfillment import Shipment,ExternalOperation
    from app.models.supply import ProcurementRequest
    factory,engine,actors=desk_db
    monkeypatch.setenv("CLIENT_ORDER_DESK_ENABLED","true")
    async def run():
        async with factory() as s,s.begin():
            s.add_all([Manager(telegram_id=a,name="SYNTHETIC",is_active=True) for a in actors])
        app=FastAPI();app.include_router(router)
        code,result,_=await request(app,"POST","/integrations/tilda/orders",payload(uuid4().hex),headers={"X-Tilda-Secret":"synthetic-tilda-secret-32-characters-only"},raise_errors=True)
        assert code==202;did=result["draft_id"]
        manager_session,client_session=TelegramSession(),TelegramSession()
        manager_bot=Bot("12345:"+"a"*35,session=manager_session)
        client_bot=Bot("67890:"+"b"*35,session=client_session)
        # Other tests share a schema: drain all prior synthetic notices before measuring.
        while await run_once(manager_bot,"manager"): pass
        assert any(f"заявка №{did}" in (getattr(c,"text","") or "") for c in manager_session.calls)
        def group_update(mid,text,actor=actors[0],chat=-100123456):
            return Update.model_validate({"update_id":mid,"message":{"message_id":mid,"date":1,
                "chat":{"id":chat,"type":"supergroup","title":"SYNTHETIC"},
                "from":{"id":actor,"is_bot":False,"first_name":"Synthetic"},"text":text}})
        def callback(mid,data,actor=actors[0],chat=-100123456):
            return Update.model_validate({"update_id":mid,"callback_query":{"id":str(mid),"chat_instance":"fake",
                "from":{"id":actor,"is_bot":False,"first_name":"Synthetic"},"data":data,
                "message":{"message_id":mid,"date":1,"chat":{"id":chat,"type":"supergroup","title":"SYNTHETIC"},"text":"Card"}}})
        await dp.feed_update(manager_bot,callback(100,f"desk:claim:{did}",chat=-999))
        async with factory() as s:assert (await s.get(OrderDesk,did)).manager_id is None
        await dp.feed_update(manager_bot,callback(101,f"desk:claim:{did}"))
        link=(await DeskService().issue_link(did))["deep_link"].split("start=")[1]
        dispatcher=create_dispatcher()
        client_id=int(uuid4().hex[:10],16)
        def client_update(mid,text):
            return Update.model_validate({"update_id":mid,"message":{"message_id":mid,"date":1,"chat":{"id":client_id,"type":"private"},
                "from":{"id":client_id,"is_bot":False,"first_name":"Synthetic"},"text":text}})
        for mid,command in enumerate(("/start", "/help", "/orders"), 10):
            await dispatcher.feed_update(client_bot,client_update(mid,command))
        async with factory() as s:
            replies=(await s.scalars(select(DeskMessage).where(DeskMessage.destination==client_id))).all()
            assert len(replies)==3
            assert all("Откройте персональную ссылку" in reply.body and reply.draft_id is None for reply in replies)
        start=client_update(1,"/start "+link)
        await dispatcher.feed_update(client_bot,start);await dispatcher.feed_update(client_bot,start)
        for mid,command in enumerate(("/help", "/orders", f"/status {did}"), 13):
            await dispatcher.feed_update(client_bot,client_update(mid,command))
        async with factory() as s:
            replies={row.idempotency_key:row for row in (await s.scalars(select(DeskMessage).where(DeskMessage.destination==client_id))).all()}
            for mid in (13,14):
                assert ("/status НОМЕР" if mid == 13 else f"/status {did}") in replies[f"desk:client:{client_id}:{mid}:ack"].body
            assert replies[f"desk:client:{client_id}:15:ack"].body==f"Заявка №{did}: В работе"
        await dispatcher.feed_update(client_bot,client_update(2,f"/message {did} <b>Здравствуйте</b>"))
        await dp.feed_update(manager_bot,group_update(201,"Internal group conversation"))
        await dp.feed_update(manager_bot,group_update(202,f"/reply {did} Wrong manager",actor=actors[1]))
        reply=group_update(203,f"/reply {did} SYNTHETIC answer <b>plain text</b>")
        await dp.feed_update(manager_bot,reply);await dp.feed_update(manager_bot,reply)
        while await run_once(manager_bot,"manager"):pass
        while await run_once(client_bot,"client"):pass
        sent=[c for c in client_session.calls if getattr(c,"chat_id",None)==client_id]
        assert sum("SYNTHETIC answer" in c.text for c in sent)==1
        assert all("Internal group" not in c.text and "Wrong manager" not in c.text and c.parse_mode is None for c in sent)
        assert sum("Спасибо за заказ" in c.text for c in sent)==1
        order=await DeskService().confirm(did,actors[0],-100123456,0)
        assert order.manual_fulfillment and order.customer_type=="retail" and order.total==3030
        assert (await DeskService().confirm(did,actors[0],-100123456,0)).id==order.id
        await DeskService().stage(did,actors[0],-100123456,"awaiting_payment")
        with pytest.raises(ManagerDenied):
            await OrderOperations().act(order.id,actors[1],OrderAction(action="paid",expected_revision=0,idempotency_key="other"))
        for action,extra in [("paid",{}),("assembling",{}),("assembled",{}),("delivery",{"delivery_method":"manual"}),("shipped",{}),("delivery",{"delivery_status":"delivered"})]:
            order=await OrderOperations().act(order.id,actors[0],OrderAction(action=action,expected_revision=order.revision,idempotency_key=uuid4().hex,**extra))
        assert order.delivery_status=="delivered" and order.payment_status=="paid"
        async with factory() as s:
            for model in (Shipment,ProcurementRequest):
                assert await s.scalar(select(func.count()).select_from(model).where(model.order_id==order.id))==0
            assert order.moysklad_order_id is None
        while await run_once(client_bot,"client"):pass
        assert any("Доставлен" in c.text for c in client_session.calls)
        await manager_bot.session.close();await client_bot.session.close();await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [("blocked","blocked"),("rate","pending"),("network","uncertain"),("bad","failed")])
def test_delivery_failure_states_and_retry(desk_db,failure,expected):
    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.exceptions import TelegramForbiddenError,TelegramRetryAfter,TelegramNetworkError,TelegramBadRequest
    from tests.telegram_transport import TelegramSession
    from app.workers.desk_outbox import run_once
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors)
        from sqlalchemy import update
        async with factory() as s,s.begin():
            # Limit this consumer to the synthetic event being tested.
            await s.execute(update(DeskMessage).where(DeskMessage.direction=="to_manager",DeskMessage.status=="pending").values(available_at=now()+timedelta(days=1)))
            m=DeskMessage(draft_id=did,idempotency_key=uuid4().hex,direction="to_manager",destination=-100123456,
                body="SYNTHETIC",sender_type="system",available_at=now())
            s.add(m);await s.flush();mid=m.id
        method=SendMessage(chat_id=-100123456,text="SYNTHETIC")
        errors={"blocked":TelegramForbiddenError(method=method,message="blocked"),
            "rate":TelegramRetryAfter(method=method,message="retry",retry_after=1),
            "network":TelegramNetworkError(method=method,message="timeout"),"bad":TelegramBadRequest(method=method,message="bad")}
        transport=TelegramSession(failure=errors[failure]);bot=Bot("12345:"+"a"*35,session=transport)
        assert await run_once(bot,"manager")
        async with factory() as s,s.begin():
            row=await s.get(DeskMessage,mid);assert row.status==expected
            if failure=="rate":row.available_at=now()-timedelta(seconds=1)
        if failure=="rate":
            assert await run_once(bot,"manager")
            async with factory() as s:
                row=await s.get(DeskMessage,mid);assert row.status=="sent" and row.attempts==2 and row.telegram_message_id
        await bot.session.close();await engine.dispose()
    asyncio.run(run())


def test_atomic_intake_rollback_and_independent_identical_orders(desk_db,monkeypatch):
    factory,engine,actors=desk_db
    async def run():
        data=normalize(payload(uuid4().hex))
        import app.services.order_desk as module
        real_enqueue=module.enqueue
        def broken(*args,**kwargs):raise RuntimeError("synthetic outbox failure")
        monkeypatch.setattr(module,"enqueue",broken)
        with pytest.raises(RuntimeError):await TildaIntake().submit(data)
        async with factory() as s:
            assert await s.scalar(select(OrderDesk).where(OrderDesk.external_id==data["external_id"])) is None
            from app.models.inbound_message import InboundMessage
            assert await s.scalar(select(InboundMessage).where(InboundMessage.external_message_id=="tilda:"+data["external_id"])) is None
        monkeypatch.setattr(module,"enqueue",real_enqueue)
        a=await TildaIntake().submit(data)
        b=await TildaIntake().submit({**data,"external_id":uuid4().hex})
        assert a["draft_id"]!=b["draft_id"]
        await engine.dispose()
    asyncio.run(run())


def test_concurrent_linking_length_attachments_and_cancel(desk_db):
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors);service=DeskService()
        link=(await service.issue_link(did))["deep_link"].split("start=")[1]
        clients=[int(uuid4().hex[:10],16) for _ in range(2)]
        await asyncio.gather(*(service.client(c,1,"/start "+link) for c in clients))
        async with factory() as s:
            desk=await s.get(OrderDesk,did);winner=desk.customer_telegram_id
            assert winner in clients
            events=(await s.scalars(select(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=="telegram_customer_linked"))).all()
            assert len(events)==1
        await service.client(winner,2,f"/message {did} "+"😀"*1501)
        await service.client(winner,3,None)
        await service.client(winner,4,"Я оплатил")
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(DeskMessage).where(DeskMessage.draft_id==did,DeskMessage.sender_type=="customer"))==0
        await service.claim(did,actors[0],-100123456)
        with pytest.raises(DeskError):await service.reply(did,actors[0],-100123456,1,"😀"*1501)
        await service.cancel(did,actors[0],-100123456)
        await service.cancel(did,actors[0],-100123456)
        with pytest.raises(DeskError):await service.confirm(did,actors[0],-100123456,1)
        async with factory() as s:
            assert (await s.get(DraftOrder,did)).status=="rejected"
            assert await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=="tilda_order_cancelled"))==1
        await engine.dispose()
    asyncio.run(run())


def test_parallel_outbox_consumers_send_once(desk_db):
    from aiogram import Bot
    from tests.telegram_transport import TelegramSession
    from app.workers.desk_outbox import run_once
    from sqlalchemy import update
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors)
        async with factory() as s,s.begin():
            await s.execute(update(DeskMessage).where(DeskMessage.direction=="to_manager",DeskMessage.status=="pending",DeskMessage.draft_id!=did).values(available_at=now()+timedelta(days=1)))
        transport=TelegramSession();bot=Bot("12345:"+"a"*35,session=transport)
        await asyncio.gather(*(run_once(bot,"manager") for _ in range(6)))
        assert len(transport.calls)==1
        async with factory() as s:
            msg=await s.scalar(select(DeskMessage).where(DeskMessage.draft_id==did))
            assert msg.status=="sent" and msg.telegram_message_id
        await bot.session.close();await engine.dispose()
    asyncio.run(run())


def test_customer_blocks_bot_and_restart_requires_reconciliation(desk_db):
    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.exceptions import TelegramForbiddenError
    from tests.telegram_transport import TelegramSession
    from app.workers.desk_outbox import run_once
    from sqlalchemy import update
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors)
        async with factory() as s,s.begin():
            await s.execute(update(DeskMessage).where(DeskMessage.direction=="to_customer",DeskMessage.status=="pending").values(available_at=now()+timedelta(days=1)))
            msg=DeskMessage(draft_id=did,idempotency_key=uuid4().hex,direction="to_customer",destination=998877,
                body="SYNTHETIC reply",sender_type="manager",sender_telegram_id=actors[0])
            lost=DeskMessage(draft_id=did,idempotency_key=uuid4().hex,direction="to_customer",destination=998877,
                body="SYNTHETIC crash",sender_type="system",status="sending",available_at=now()-timedelta(seconds=1))
            s.add_all([msg,lost]);await s.flush();mid,lid=msg.id,lost.id
        transport=TelegramSession(failure=TelegramForbiddenError(method=SendMessage(chat_id=998877,text="synthetic"),message="blocked"))
        bot=Bot("67890:"+"b"*35,session=transport)
        assert await run_once(bot,"client")
        assert not await run_once(bot,"client")
        async with factory() as s:
            assert (await s.get(DeskMessage,mid)).status=="blocked"
            assert (await s.get(DeskMessage,lid)).status=="uncertain"
            alarm=await s.scalar(select(DeskMessage).where(DeskMessage.idempotency_key==f"delivery-failed:{mid}:1"))
            assert alarm.direction=="to_manager" and "blocked" in alarm.body and "SYNTHETIC reply" not in alarm.body
        await bot.session.close();await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["pending","sending","uncertain","failed","blocked"])
def test_staging_allowlist_leaves_other_recipients_and_unapproved_rows_untouched(desk_db,monkeypatch,state):
    from app.config.settings import settings
    from app.workers.desk_outbox import run_once
    from aiogram import Bot
    from tests.telegram_transport import TelegramSession
    factory,engine,actors=desk_db
    monkeypatch.setattr(settings,"environment","staging")
    monkeypatch.setenv("ORDER_DESK_SEND_ENABLED","true")
    monkeypatch.setenv("ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS","898019732")
    monkeypatch.delenv("ORDER_DESK_STAGING_DRAFT_IDS",raising=False)
    async def run():
        async with factory() as s,s.begin():
            selected=DeskMessage(idempotency_key=uuid4().hex,direction="to_customer",destination=898019732,
                body="SYNTHETIC allowed",sender_type="system",status="pending")
            outsider=DeskMessage(idempotency_key=uuid4().hex,direction="to_customer",destination=333,
                body="SYNTHETIC excluded",sender_type="system",status=state,available_at=now()-timedelta(minutes=10))
            unselected=DeskMessage(idempotency_key=uuid4().hex,direction="to_customer",destination=898019732,
                body="SYNTHETIC unapproved",sender_type="system",status=state,available_at=now()-timedelta(minutes=10))
            s.add_all([selected,outsider,unselected]);await s.flush()
            chosen,other,unapproved=selected.id,outsider.id,unselected.id
        # Explicit message ID still cannot authorize an unapproved recipient.
        monkeypatch.setenv("ORDER_DESK_STAGING_MESSAGE_IDS",f"{chosen},{other}")
        transport=TelegramSession();bot=Bot("67890:"+"b"*35,session=transport)
        assert await run_once(bot,"client")
        assert not await run_once(bot,"client")
        assert len(transport.calls)==1 and transport.calls[0].chat_id==898019732
        async with factory() as s:
            assert (await s.get(DeskMessage,chosen)).status=="sent"
            for mid in (other,unapproved):
                row=await s.get(DeskMessage,mid)
                assert row.status==state and row.attempts==0 and row.telegram_message_id is None
        await bot.session.close();await engine.dispose()
    asyncio.run(run())


def test_staging_default_deny_does_not_mutate_queue(desk_db,monkeypatch):
    from app.config.settings import settings
    from app.workers.desk_outbox import run_once
    factory,engine,actors=desk_db
    monkeypatch.setattr(settings,"environment","staging")
    for suffix in ("CLIENT_RECIPIENT_IDS","MANAGER_CHAT_IDS","MESSAGE_IDS","DRAFT_IDS"):
        monkeypatch.delenv("ORDER_DESK_STAGING_"+suffix,raising=False)
    async def run():
        async with factory() as s:
            before=(await s.execute(select(DeskMessage.id,DeskMessage.status,DeskMessage.attempts).order_by(DeskMessage.id))).all()
        bot=SimpleNamespace(send_message=AsyncMock())
        assert not await run_once(bot,"client")
        assert not await run_once(bot,"manager")
        bot.send_message.assert_not_awaited()
        async with factory() as s:
            after=(await s.execute(select(DeskMessage.id,DeskMessage.status,DeskMessage.attempts).order_by(DeskMessage.id))).all()
            assert before==after
        await engine.dispose()
    asyncio.run(run())
