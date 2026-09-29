"""Read exact linked Gmail threads, independent of customer intake selectors."""
import asyncio
from datetime import datetime, timezone
from email.utils import parseaddr
from sqlalchemy import select
from app.database.session import async_session
from app.models.buying import BuyingPurchase
from app.services.buying import ingest_reply
from app.config.settings import settings


class SupplierReplyPoller:
    def __init__(self, provider):
        if getattr(provider, 'mailbox_role', None) != 'supplier':
            raise ValueError('Supplier replies require SUPPLIER Gmail only')
        if not provider.account:
            raise ValueError('Supplier mailbox identity must be configured')
        self.provider = provider
        self.after_id = 0

    async def run_once(self):
        async with async_session() as session:
            rows = (await session.execute(select(BuyingPurchase.id, BuyingPurchase.thread_id).where(
                BuyingPurchase.thread_id.is_not(None), ~BuyingPurchase.thread_id.like('fake-%'),
                BuyingPurchase.supplier_mailbox_account == self.provider.account,
                BuyingPurchase.id > self.after_id).order_by(BuyingPurchase.id).limit(50))).all()
        if not rows:
            self.after_id = 0
            return 0
        count = 0
        for pid, thread in rows:
            raw = await asyncio.to_thread(lambda: self.provider.service.users().threads().get(
                userId=self.provider.user_id, id=thread, format='full').execute())
            for message in raw.get('messages',[]):
                payload = message.get('payload',{})
                headers = {h['name'].casefold():h['value'] for h in payload.get('headers',[])}
                attachments = []
                def visit(part, depth=0):
                    if depth>30:
                        return
                    if part.get('filename'):
                        attachments.append({'filename':part['filename'][:255], 'mime_type':part.get('mimeType','')[:100], 'size':part.get('body',{}).get('size',0)})
                    for child in part.get('parts',[]):
                        visit(child,depth+1)
                visit(payload)
                async with async_session() as session, session.begin():
                    reply = await ingest_reply(session,message_id=message['id'],thread_id=thread,
                        sender=parseaddr(headers.get('from',''))[1],
                        mailbox_account=self.provider.account,
                        received_at=datetime.fromtimestamp(int(message.get('internalDate','0'))/1000,timezone.utc),
                        subject=headers.get('subject',''), body=self.provider._body_text(payload), attachments=attachments)
                    count += bool(reply)
            self.after_id = pid
        return count
