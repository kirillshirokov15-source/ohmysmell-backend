import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from app.services.email_parser import EmailParser
from app.bot.manager_group import allowed_event, notification_chats
from tests.test_buying import workspace


@pytest.mark.parametrize('text,qty,confidence',[
    ('Chanel 100 ml x3',3,'confirmed'), ('Chanel 100 ml 3 шт',3,'confirmed'),
    ('Chanel 100 ml qty 3',3,'confirmed'), ('Chanel 100 ml\t3',3,'probable'),
    ('Product\tQuantity\nChanel 100 ml\t3',3,'confirmed'),
    ('Chanel 100 ml\n3',3,'probable'), ('Chanel 100 ml',0,'unknown'),
    ('Итого 15 шт\nChanel 100 ml',0,'unknown'),
    ('<table><tr><th>Product</th><th>Qty</th></tr><tr><td>Chanel 100 ml</td><td>3</td></tr></table>',3,'confirmed'),
    ('Chanel 100 ml x0',0,'unknown'),
])
def test_quantity_confidence(text,qty,confidence):
    rows=EmailParser().parse_lines(text)
    assert len(rows)==1
    assert (rows[0].qty,rows[0].quantity_confidence)==(qty,confidence)
    assert rows[0].quantity_evidence


def test_no_header_total_or_greeting_as_item():
    assert EmailParser().parse_lines('Здравствуйте\nИтого 12 шт\nСпасибо')==[]


def test_shared_group_access(monkeypatch):
    monkeypatch.setenv('MANAGER_TELEGRAM_CHAT_ID','-100123')
    monkeypatch.setenv('MANAGER_TELEGRAM_USER_IDS','11,12')
    message=NS(chat=NS(id=-100123,type='supergroup'),from_user=NS(id=11),text='/orders')
    assert allowed_event(message)
    message.text='normal conversation'; assert not allowed_event(message)
    callback=NS(message=message,from_user=NS(id=12))
    assert allowed_event(callback,True)
    callback.from_user.id=13; assert not allowed_event(callback,True)
    callback.from_user.id=11; message.chat.id=-100124; assert not allowed_event(callback,True)
    assert notification_chats([NS(telegram_id=11),NS(telegram_id=12)])==[-100123]
    message.chat.id=-100123;message.text='/orders';message.sender_chat=NS(id=-100123)
    assert not allowed_event(message)


def test_quantity_correction_audit_and_finalize_guard(workspace,monkeypatch):
    _,factory,_=workspace
    from app.repositories import draft_order_repository as repo
    from app.models.customer import Customer
    from app.models.manager import Manager
    from app.models.draft_order import DraftQuantityEvent
    from app.services.order_lifecycle import InvalidOrderTransitionError
    monkeypatch.setattr(repo,'async_session',factory)
    async def run():
        async with factory() as s,s.begin():
            customer=Customer(customer_type='wholesale',display_name='SYNTHETIC');s.add(customer)
            s.add(Manager(telegram_id=11,name='SYNTHETIC',is_active=True));await s.flush()
        r=repo.DraftOrderRepository()
        from app.services.buying import now
        draft=await r.create(dict(source='email',external_message_id='quantity-case',sender_email='test@example.invalid',body_text='Product',received_at=now(),customer_id=customer.id,
            customer_type='wholesale',counterparty_id='synthetic',status='ready',total=100,
            items=[dict(raw_product_text='Product',qty=1,quantity_confidence='probable',match_status='matched',product_id='test',price=100,item_total=100)]))
        with pytest.raises(InvalidOrderTransitionError):await r.finalize(draft.id)
        with pytest.raises(InvalidOrderTransitionError):await r.correct_quantity(draft.id,draft.items[0].id,3,99)
        r.expected_revision=draft.revision
        changed=await r.correct_quantity(draft.id,draft.items[0].id,3,11)
        assert changed.items[0].quantity_confidence=='confirmed' and changed.items[0].qty==3
        assert changed.total is None
        with pytest.raises(InvalidOrderTransitionError):await r.correct_quantity(draft.id,draft.items[0].id,4,11)
        async with factory() as s:
            event=await s.scalar(select(DraftQuantityEvent))
            assert (event.old_quantity,event.new_quantity,event.actor_telegram_id,event.old_confidence)==(1,3,11,'probable')
    asyncio.run(run())


def test_buying_group_events_delivered_once(workspace,monkeypatch):
    _,factory,_=workspace
    from app.workers import buying_notifications as worker
    from app.models.buying import BuyingEvent,BuyingPurchase
    from app.models.supply import Supplier
    monkeypatch.setattr(worker,'async_session',factory)
    monkeypatch.setenv('MANAGER_TELEGRAM_CHAT_ID','-100123')
    sender=AsyncMock()
    async def run():
        async with factory() as s,s.begin():
            supplier=Supplier(name='SYNTHETIC',supplier_type='external_wholesaler');s.add(supplier);await s.flush()
            purchase=BuyingPurchase(supplier_id=supplier.id,snapshot={},status='sent',send_state='simulated');s.add(purchase);await s.flush()
            s.add_all([BuyingEvent(purchase_id=purchase.id,action=action,actor='test') for action in ('created','supplier_reply')])
        assert await worker.run_once(sender)
        assert await worker.run_once(sender)
        assert not await worker.run_once(sender)
        assert sender.await_count==2
    asyncio.run(run())


def test_supplier_readonly_poller_any_reply_including_archived(workspace,monkeypatch):
    _,factory,_=workspace
    from app.workers import supplier_replies as worker
    from app.models.buying import BuyingPurchase,BuyingReply
    from app.models.supply import Supplier
    from app.integrations.email.gmail_provider import GmailEmailProvider
    import base64
    monkeypatch.setattr(worker,'async_session',factory)
    raw={'messages':[{'id':'reply-archived','internalDate':'1800000000000','payload':{
        'headers':[{'name':'From','value':'supplier@example.invalid'},{'name':'Subject','value':'Any question'}],
        'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'When can you collect?').decode()}}}]}
    class ThreadAPI:
        def users(self):return self
        def threads(self):return self
        def get(self,**kw):
            assert kw['id']=='real-linked-thread' and kw['format']=='full'
            return self
        def execute(self):return raw
    poller=worker.SupplierReplyPoller(GmailEmailProvider(service=ThreadAPI()))
    async def run():
        async with factory() as s,s.begin():
            supplier=Supplier(name='SYNTHETIC',email='supplier@example.invalid',supplier_type='external_wholesaler');s.add(supplier);await s.flush()
            s.add(BuyingPurchase(supplier_id=supplier.id,snapshot={},status='sent',send_state='sent',thread_id='real-linked-thread',message_id='outbound',external_ids={}))
        assert await poller.run_once()==1
        assert await poller.run_once()==0
        assert await poller.run_once()==1
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingReply))==1
            reply=await s.get(BuyingReply,'reply-archived')
            assert reply.body=='When can you collect?'
    asyncio.run(run())
