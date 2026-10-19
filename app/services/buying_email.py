"""Durable supplier send intent, exclusive purchase transition and explicit reconciliation."""
from app.database.session import async_session
from app.config.settings import settings
from app.models.buying import BuyingEvent
from app.services import buying
from app.services.external_operation_service import ExternalOperationService
from app.integrations.email.supplier_sender import FakeGmailSupplierSender, GmailSupplierSender
from app.integrations.write_guard import require_external_writes


class BuyingEmailService:
    def __init__(self, sender=None, operations=None, sessions=None):
        self.sender=sender or GmailSupplierSender()
        self.operations=operations or ExternalOperationService()
        self.sessions=sessions or async_session

    async def act(self, identifier, actor, *, reconcile=False):
        fake=type(self.sender) is FakeGmailSupplierSender
        if fake:
            if settings.environment=='production':
                raise ValueError('Fake email forbidden in production')
        else:
            require_external_writes()
            if not settings.supplier_email_send_enabled:
                raise ValueError('Supplier sending disabled')
        key=('simulation:' if fake else '')+f'supplier-email:{identifier}'
        async with self.sessions() as session, session.begin():
            await buying.lock(session)
            p=await buying.purchase_get(session,identifier)
            if p.status in {'sent','confirmed','received'}:
                return buying.purchase_view(p)
            if p.status!='draft' or (p.send_state in {'inflight','uncertain'} and not reconcile):
                buying.fail('Purchase requires reconciliation or is not a draft')
            intent={'key':key,'account':self.sender.account,'recipient':p.snapshot['recipient'],
                    'items':[{'name':i['name'],'quantity':i['quantity']} for i in p.snapshot['items']]}
            if reconcile:
                previous=await self.operations.store.get(key)
                if not previous or previous['payload']!=intent:
                    buying.fail('Sending intent not found or changed')
            else:
                p.send_state='inflight'
                p.supplier_mailbox_account=self.sender.account
                session.add(BuyingEvent(purchase_id=p.id,action='email_intent',actor=actor))
        try:
            if reconcile:
                result=previous['result'] if previous['status']=='succeeded' else await self.sender.find(intent)
                if not result:
                    buying.fail('No exact email receipt; automatic resend forbidden')
                await self.operations.store.finish(key,result)
            else:
                # Fake sender is an exact local class; live transport has both guards.
                result=await self.operations._execute(key,intent,self.sender.send)
        except BaseException:
            async with self.sessions() as session, session.begin():
                await buying.lock(session)
                p=await buying.purchase_get(session,identifier)
                if p.status=='draft':
                    p.send_state='uncertain'
            raise
        async with self.sessions() as session, session.begin():
            await buying.lock(session)
            p=await buying.purchase_get(session,identifier)
            if p.status=='draft':
                p.status='sent'; p.send_state='simulated' if fake else 'sent'
                p.message_id=result['id']; p.thread_id=result['thread_id']; p.sent_at=buying.now()
                session.add(BuyingEvent(purchase_id=p.id,action='email_simulated' if fake else 'email_sent',actor=actor))
            await session.flush(); await session.refresh(p)
            return buying.purchase_view(p)
