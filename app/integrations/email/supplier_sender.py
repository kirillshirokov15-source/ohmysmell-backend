"""Explicit supplier Gmail send/reconciliation, never customer mailbox credentials."""
import asyncio
import base64
import hashlib
from email.message import EmailMessage
from email.policy import SMTP
from app.integrations.write_guard import require_external_writes
from app.integrations.email.mailboxes import mailbox, verify_identity, expected_email
from app.config.settings import settings


def message_id(key):
    return hashlib.sha256(key.encode()).hexdigest() + '@ohmysmell.invalid'


def build_email(key, recipient, items):
    if '\n' in recipient or '\r' in recipient or '@' not in recipient:
        raise ValueError('Invalid supplier recipient')
    lines=[]
    for item in items:
        name=item['name']
        qty=item['quantity']
        if not name or any(ord(c)<32 for c in name) or type(qty) is not int or qty<=0:
            raise ValueError('Invalid purchase line')
        lines.append(f'{name} — {qty} шт.')
    if not lines:
        raise ValueError('Empty purchase')
    msg=EmailMessage(policy=SMTP)
    msg['To']=recipient
    msg['Subject']='Заказ'
    msg['Message-ID']='<'+message_id(key)+'>'
    msg.set_content('\n'.join(lines))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailSupplierSender:
    def __init__(self, service=None):
        self.service=service
        self.account=expected_email('supplier')

    def _service(self):
        if self.service is None:
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
            import httplib2
            from app.integrations.email.gmail_auth import load_gmail_credentials
            from app.integrations.http_tls import verified_ca_bundle
            credentials=load_gmail_credentials(allow_interactive=False,mailbox_role='supplier')
            self.service=build('gmail','v1',http=AuthorizedHttp(credentials,http=httplib2.Http(
                timeout=20,ca_certs=verified_ca_bundle())),cache_discovery=False)
        verify_identity('supplier',self.service.users().getProfile(userId=mailbox('supplier').user_id).execute())
        return self.service

    async def send(self, intent):
        require_external_writes()
        if not settings.supplier_email_send_enabled:
            raise ValueError('Supplier sending disabled')
        raw=build_email(intent['key'],intent['recipient'],intent['items'])
        def run():
            result=self._service().users().messages().send(userId=mailbox('supplier').user_id,body={'raw':raw}).execute()
            if not result.get('id') or not result.get('threadId'):
                raise ValueError('Incomplete Gmail receipt')
            return {'id':result['id'],'thread_id':result['threadId'],'simulated':False}
        return await asyncio.to_thread(run)

    async def find(self, intent):
        def run():
            api=self._service().users().messages()
            matches=api.list(userId=mailbox('supplier').user_id,q='in:sent rfc822msgid:'+message_id(intent['key']),maxResults=2).execute()
            if matches.get('nextPageToken') or len(matches.get('messages',[]))!=1:
                return None
            found=api.get(userId=mailbox('supplier').user_id,id=matches['messages'][0]['id'],format='raw').execute()
            from email import policy
            from email.parser import BytesParser
            msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(found['raw']))
            expected=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(build_email(intent['key'],intent['recipient'],intent['items'])))
            if (str(msg['To'])!=intent['recipient'] or str(msg['Message-ID'])!=str(expected['Message-ID'])
                    or msg.get_body(preferencelist=('plain',)).get_content().strip()!=expected.get_content().strip()):
                return None
            return {'id':found['id'],'thread_id':found['threadId'],'simulated':False}
        return await asyncio.to_thread(run)


class FakeGmailSupplierSender:
    account='supplier@example.invalid'
    def __init__(self):
        self.messages={}
        self.calls=0
        self.lose_response=False
    async def send(self,intent):
        build_email(intent['key'],intent['recipient'],intent['items'])
        self.calls+=1
        result={'id':'fake-message:'+intent['key'],'thread_id':'fake-thread:'+intent['key'],'simulated':True}
        self.messages[intent['key']]=result
        if self.lose_response:
            raise TimeoutError('Simulated response loss')
        return result
    async def find(self,intent):
        return self.messages.get(intent['key'])
