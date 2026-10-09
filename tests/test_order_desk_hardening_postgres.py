"""Isolated local PostgreSQL regressions; Telegram is always a fake transport."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from tests.test_tilda_postgres import desk_db, seed, staging_sessions, tilda_config
from app.models.draft_order import DraftOrder
from app.models.order import Order
from app.models.order_desk import OrderDesk, DeskEvent, DeskMessage
from app.models.operations import OrderEvent
from app.services.order_desk import DeskService, DeskError, public_status, now
from app.services.order_operations import OrderOperations, OrderAction, OperationError
from app.services.desk_display import desk_card, desk_history

pytestmark=pytest.mark.staging
CHAT=-100123456


async def confirmed(factory,actors):
    did=await seed(factory,actors);service=DeskService()
    await service.claim(did,actors[0],CHAT)
    customer=int(uuid4().hex[:10],16)
    link=(await service.issue_link(did))['deep_link'].split('start=')[1]
    await service.client(customer,1,'/start '+link)
    order=await service.confirm(did,actors[0],CHAT,0)
    await service.stage(did,actors[0],CHAT,'awaiting_payment')
    return did,order,customer,link


def test_confirmed_lifecycle_projects_to_desk_client_and_order_without_backfill(desk_db):
    from app.services.manager_workspace import order_card
    from app.models.fulfillment import Shipment,ExternalOperation
    from app.models.supply import ProcurementRequest
    factory,engine,actors=desk_db
    async def run():
        async with factory() as s:
            external_before=await s.scalar(select(func.count()).select_from(ExternalOperation))
        did,order,customer,link=await confirmed(factory,actors)
        ops=OrderOperations();service=DeskService()
        steps=[('paid',{},'Оплачен'),('assembling',{},'В сборке'),('assembled',{},'Собран'),
            ('delivery',{'delivery_method':'manual'},'Собран'),('shipped',{},'Отгружен'),
            ('delivery',{'delivery_status':'delivered'},'Доставлен')]
        for action,extra,label in steps:
            request=OrderAction(action=action,expected_revision=order.revision,idempotency_key=uuid4().hex,**extra)
            order=await ops.act(order.id,actors[0],request)
            assert (await ops.act(order.id,actors[0],request)).revision==order.revision
            card,kb=await desk_card(did)
            async with factory() as s:
                desk=await s.get(OrderDesk,did)
                assert desk.stage=='awaiting_payment'  # Historical storage untouched.
                public=await public_status(s,desk)
            assert label in public and f'Этап: {label}' in card and label in order_card(order)
            assert 'Ожидает оплаты' not in public+card
            assert not any(b.callback_data in (f'desk:payment:{did}',f'desk:claim:{did}') for row in kb.inline_keyboard for b in row)
            with pytest.raises(OperationError):
                await ops.act(order.id,actors[0],OrderAction(action='cancel',expected_revision=order.revision,idempotency_key=uuid4().hex))
        with pytest.raises(DeskError):await service.stage(did,actors[0],CHAT,'awaiting_payment')
        await service.client(customer,2,'/start '+link)
        await service.client(customer,3,'/orders')
        async with factory() as s:
            for mid in (2,3):
                ack=await s.scalar(select(DeskMessage).where(DeskMessage.idempotency_key==f'desk:client:{customer}:{mid}:ack'))
                assert 'Доставлен' in ack.body and 'Ожидает оплаты' not in ack.body
            assert await s.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id==order.id))==6
            assert await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=='tilda_review_approved'))==1
            for model in (Shipment,ProcurementRequest):
                assert await s.scalar(select(func.count()).select_from(model).where(model.order_id==order.id))==0
            assert await s.scalar(select(func.count()).select_from(ExternalOperation))==external_before
        await engine.dispose()
    asyncio.run(run())


def test_confirmation_failure_rolls_back_approval_order_and_outbox(desk_db,monkeypatch):
    import app.services.order_desk as module
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors);service=DeskService();await service.claim(did,actors[0],CHAT)
        real=module.status_notification
        monkeypatch.setattr(module,'status_notification',AsyncMock(side_effect=RuntimeError('SYNTHETIC commit failure')))
        with pytest.raises(RuntimeError):await service.confirm(did,actors[0],CHAT,0)
        async with factory() as s:
            draft=await s.get(DraftOrder,did);desk=await s.get(OrderDesk,did)
            assert draft.status=='needs_review' and draft.finalized_order_id is None and desk.order_id is None
            assert not draft.contact_details.get('tilda_review_approved')
            assert await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=='tilda_review_approved'))==0
        monkeypatch.setattr(module,'status_notification',real)
        orders=await asyncio.gather(*(service.confirm(did,actors[0],CHAT,0) for _ in range(3)))
        assert len({o.id for o in orders})==1
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did,DeskEvent.action=='tilda_review_approved'))==1
        await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('quantity_confidence','unknown'),('match_status','ambiguous'),('product_id',None),('price',None),('price',0)])
def test_unresolved_item_cannot_be_approved(desk_db,field,value):
    factory,engine,actors=desk_db
    async def run():
        did=await seed(factory,actors);service=DeskService();await service.claim(did,actors[0],CHAT)
        async with factory() as s,s.begin():
            draft=await s.scalar(select(DraftOrder).where(DraftOrder.id==did).options(selectinload(DraftOrder.items)))
            setattr(draft.items[0],field,value)
        with pytest.raises(DeskError):await service.confirm(did,actors[0],CHAT,0)
        async with factory() as s:
            assert (await s.get(OrderDesk,did)).order_id is None
            assert (await s.get(DraftOrder,did)).status=='needs_review'
        await engine.dispose()
    asyncio.run(run())


def test_cancelled_order_projection_and_stale_concurrent_operations(desk_db):
    factory,engine,actors=desk_db
    async def run():
        did,order,customer,_=await confirmed(factory,actors);ops=OrderOperations()
        requests=[OrderAction(action='paid',expected_revision=0,idempotency_key=uuid4().hex),
                  OrderAction(action='cancel',expected_revision=0,idempotency_key=uuid4().hex)]
        results=await asyncio.wait_for(asyncio.gather(*(ops.act(order.id,actors[0],r) for r in requests),return_exceptions=True),10)
        assert sum(isinstance(r,OperationError) for r in results)==1
        async with factory() as s:
            actual=await s.get(Order,order.id);desk=await s.get(OrderDesk,did)
            assert actual.revision==1
            expected='Отменён' if actual.fulfillment_status=='cancelled' else 'Оплачен'
            assert expected in await public_status(s,desk)
        with pytest.raises(OperationError):
            await ops.act(order.id,actors[0],OrderAction(action='assembling',expected_revision=0,idempotency_key=uuid4().hex))
        await engine.dispose()
    asyncio.run(run())


def test_payment_and_desk_stage_race_uses_order_as_authority(desk_db):
    factory,engine,actors=desk_db
    async def run():
        did,order,customer,_=await confirmed(factory,actors)
        result=await asyncio.wait_for(asyncio.gather(
            DeskService().stage(did,actors[0],CHAT,'awaiting_payment'),
            OrderOperations().act(order.id,actors[0],OrderAction(action='paid',expected_revision=0,idempotency_key=uuid4().hex)),
            return_exceptions=True),10)
        assert not any(isinstance(x,Exception) and not isinstance(x,DeskError) for x in result)
        assert 'Оплачен' in (await desk_card(did))[0]
        assert 'Ожидает оплаты' not in (await desk_card(did))[0]
        await engine.dispose()
    asyncio.run(run())


def test_unpaid_confirmed_order_can_be_cancelled_idempotently(desk_db):
    factory,engine,actors=desk_db
    async def run():
        did,order,customer,_=await confirmed(factory,actors)
        action=OrderAction(action='cancel',expected_revision=0,idempotency_key=uuid4().hex)
        for _ in range(2):await OrderOperations().act(order.id,actors[0],action)
        text,kb=await desk_card(did)
        assert 'Этап: Отменён' in text
        assert not any(b.callback_data==f'desk:payment:{did}' for row in kb.inline_keyboard for b in row)
        async with factory() as s:
            assert 'Отменён' in await public_status(s,await s.get(OrderDesk,did))
            assert await s.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id==order.id))==1
        await engine.dispose()
    asyncio.run(run())


def test_missing_failure_alarm_route_does_not_lose_delivery_result(desk_db,monkeypatch):
    from app.config.settings import settings
    from app.workers.desk_outbox import run_once
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage
    factory,engine,actors=desk_db
    monkeypatch.setattr(settings,'environment','staging')
    monkeypatch.delenv('ORDER_DESK_STAGING_DRAFT_IDS',raising=False)
    async def run():
        did,order,customer,_=await confirmed(factory,actors)
        reply=await DeskService().reply(did,actors[0],CHAT,70,'SYNTHETIC reply')
        monkeypatch.setenv('ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS',str(customer))
        monkeypatch.setenv('ORDER_DESK_STAGING_MESSAGE_IDS',str(reply.id))
        monkeypatch.delenv('MANAGER_TELEGRAM_CHAT_ID')
        error=TelegramForbiddenError(method=SendMessage(chat_id=customer,text='SYNTHETIC'),message='SYNTHETIC blocked')
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=error))
        assert await run_once(bot,'client')
        async with factory() as s:
            row=await s.get(DeskMessage,reply.id)
            assert row.status=='blocked' and row.attempts==1
        await engine.dispose()
    asyncio.run(run())


def test_reopening_staging_window_excludes_old_draft_and_unlinked_messages(desk_db,monkeypatch):
    from app.config.settings import settings
    from app.workers.desk_outbox import run_once
    factory,engine,actors=desk_db
    monkeypatch.setattr(settings,'environment','staging')
    async def run():
        did=await seed(factory,actors);boundary=now();customer=int(uuid4().hex[:10],16)
        ids=[]
        async with factory() as s,s.begin():
            for draft,stamp,state in [(did,boundary-timedelta(days=1),'pending'),(None,boundary,'pending'),
                    (did,boundary,'pending'),(did,boundary,'sending'),(did,boundary,'uncertain')]:
                row=DeskMessage(draft_id=draft,idempotency_key=uuid4().hex,direction='to_customer',destination=customer,
                    sender_type='system',body='SYNTHETIC',created_at=stamp,status=state,available_at=boundary-timedelta(minutes=10))
                s.add(row);await s.flush();ids.append(row.id)
        for key,value in {'CLIENT_RECIPIENT_IDS':str(customer),'DRAFT_IDS':str(did),'MESSAGE_IDS':'','NOT_BEFORE':boundary.isoformat()}.items():
            monkeypatch.setenv('ORDER_DESK_STAGING_'+key,value)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)))
        monkeypatch.setenv('ORDER_DESK_SEND_ENABLED','false')
        assert not await run_once(bot,'client')
        monkeypatch.setenv('ORDER_DESK_SEND_ENABLED','true')
        assert await run_once(bot,'client')
        assert not await run_once(bot,'client')
        assert bot.send_message.await_count==1
        async with factory() as s:
            saved=[await s.get(DeskMessage,mid) for mid in ids]
            assert [r.status for r in saved]==['pending','pending','sent','uncertain','uncertain']
            assert saved[0].attempts==saved[1].attempts==0
        # Only an explicit, independently approved message ID can include history.
        monkeypatch.setenv('ORDER_DESK_STAGING_MESSAGE_IDS',str(ids[0]))
        assert await run_once(bot,'client')
        assert bot.send_message.await_count==2
        await engine.dispose()
    asyncio.run(run())


def test_late_telegram_result_cannot_overwrite_reconciliation(desk_db,monkeypatch):
    from app.config.settings import settings
    from app.workers.desk_outbox import run_once
    factory,engine,actors=desk_db
    monkeypatch.setattr(settings,'environment','staging')
    monkeypatch.delenv('ORDER_DESK_STAGING_DRAFT_IDS',raising=False)
    async def run():
        customer=int(uuid4().hex[:10],16)
        async with factory() as s,s.begin():
            row=DeskMessage(idempotency_key=uuid4().hex,direction='to_customer',destination=customer,sender_type='system',body='SYNTHETIC')
            s.add(row);await s.flush();mid=row.id
        monkeypatch.setenv('ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS',str(customer))
        monkeypatch.setenv('ORDER_DESK_STAGING_MESSAGE_IDS',str(mid))
        async def send(*args,**kwargs):
            async with factory() as s,s.begin():
                row=await s.get(DeskMessage,mid);row.status='sent';row.telegram_message_id=999
            return SimpleNamespace(message_id=100)
        assert await run_once(SimpleNamespace(send_message=send),'client')
        async with factory() as s:
            assert (await s.get(DeskMessage,mid)).telegram_message_id==999
        await engine.dispose()
    asyncio.run(run())


def test_manager_help_history_and_client_blank_input_are_authorized(desk_db):
    from aiogram import Bot
    from aiogram.types import Update
    from tests.telegram_transport import TelegramSession
    from app.bot.telegram_bot import dp
    factory,engine,actors=desk_db
    async def run():
        did,order,customer,_=await confirmed(factory,actors)
        transport=TelegramSession();bot=Bot('12345:'+'a'*35,session=transport)
        def help_update(chat,actor):
            return Update.model_validate({'update_id':1,'message':{'message_id':1,'date':1,
                'chat':{'id':chat,'type':'supergroup'},'from':{'id':actor,'is_bot':False,'first_name':'Synthetic'},'text':'/help'}})
        await dp.feed_update(bot,help_update(CHAT,actors[0]))
        assert '/site НОМЕР' in transport.calls[-1].text and '/procurements' in transport.calls[-1].text
        count=len(transport.calls)
        await dp.feed_update(bot,help_update(-999,actors[0]))
        await dp.feed_update(bot,help_update(CHAT,333))
        assert len(transport.calls)==count
        async with factory() as s:
            before=await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did))
        text=await desk_history(did)
        assert 'История заявки' in text and 'Товары и сумма согласованы' in text
        await DeskService().client(customer,90,'   ')
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(DeskEvent).where(DeskEvent.draft_id==did))==before
            response=await s.scalar(select(DeskMessage).where(DeskMessage.idempotency_key==f'desk:client:{customer}:90:ack'))
            assert 'Сообщение пустое' in response.body
        await bot.session.close();await engine.dispose()
    asyncio.run(run())
