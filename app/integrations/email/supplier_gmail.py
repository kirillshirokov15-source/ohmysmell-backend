"""Supplier-only Gmail reader. It has no wholesale intake method or customer cursor."""
from app.integrations.email.gmail_provider import GmailEmailProvider, ReadOnlyHttp
from app.integrations.email.gmail_auth import load_gmail_credentials
from app.integrations.email.mailboxes import mailbox, verify_identity, expected_email


class SupplierGmailProvider:
    mailbox_role = 'supplier'
    _body_text = staticmethod(GmailEmailProvider._body_text)

    def __init__(self, service=None):
        self.service = service
        self.user_id = mailbox('supplier').user_id
        self.account = expected_email('supplier')

    def _build_service(self):
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
        from app.integrations.http_tls import verified_ca_bundle
        credentials = load_gmail_credentials(allow_interactive=False, mailbox_role='supplier')
        transport = AuthorizedHttp(credentials, http=httplib2.Http(timeout=20, ca_certs=verified_ca_bundle()))
        service = build('gmail', 'v1', http=ReadOnlyHttp(transport), cache_discovery=False)
        verify_identity('supplier', service.users().getProfile(userId=self.user_id).execute())
        return service

    def readonly_smoke_check(self, max_messages=5):
        service = self.service or self._build_service()
        account = verify_identity('supplier', service.users().getProfile(userId=self.user_id).execute())
        messages = service.users().messages().list(userId=self.user_id, labelIds=['INBOX'], maxResults=max(1,min(5,max_messages))).execute()
        return dict(account=account, messages_found=len(messages.get('messages', [])), send_scope_present=True, email_sent=False)

    def price_attachments(self, page_token=None):
        import base64
        import re
        from email.utils import parseaddr
        from app.services.buying_excel import MAX_UPLOAD
        service = self.service or self._build_service()
        verify_identity('supplier', service.users().getProfile(userId=self.user_id).execute())
        api=service.users().messages()
        page=api.list(userId=self.user_id,q='has:attachment (filename:xlsx OR filename:csv)',
                      maxResults=25,**({'pageToken':page_token} if page_token else {})).execute()
        attachments=[]
        for entry in page.get('messages',[]):
            message=api.get(userId=self.user_id,id=entry['id'],format='full').execute()
            payload=message.get('payload',{})
            headers=payload.get('headers',[])
            senders=[h['value'] for h in headers if h['name'].casefold()=='from']
            if len(senders)!=1:
                continue
            sender=parseaddr(senders[0])[1].casefold()
            domain=sender.rsplit('@',1)[-1]
            # Fail closed on unverified/forwarded sender identity; no fuzzy mapping.
            authentication=[h['value'] for h in headers if h['name'].casefold()=='authentication-results'
                            and h['value'].lstrip().startswith('mx.google.com;')]
            auth=(len(authentication)==1 and re.search(r'\bdmarc=pass\b',authentication[0]) and
                  re.search(r'\bheader\.from='+re.escape(domain)+r'(?:\s|;|$)',authentication[0]))
            if not auth:
                continue
            def visit(part, depth=0):
                if depth>20:
                    raise ValueError('Attachment nesting exceeded')
                filename=part.get('filename','')
                body=part.get('body',{})
                aid=body.get('attachmentId')
                if filename.lower().endswith(('.xlsx','.csv')) and aid:
                    if len(attachments)>=50:
                        raise ValueError('Attachment count exceeds page limit')
                    if not 0<body.get('size',0)<=MAX_UPLOAD:
                        raise ValueError('Price attachment exceeds size limit')
                    raw=api.attachments().get(userId=self.user_id,messageId=entry['id'],id=aid).execute()
                    encoded=raw.get('data','')
                    if len(encoded)>MAX_UPLOAD*2:
                        raise ValueError('Attachment response exceeds size limit')
                    data=base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4))
                    if len(data)>MAX_UPLOAD:
                        raise ValueError('Attachment exceeds size limit')
                    attachments.append(dict(account=self.account,message_id=entry['id'],attachment_id=aid,
                        sender=sender,authenticated=True,filename=filename,data=data))
                for child in part.get('parts',[]):
                    visit(child,depth+1)
            visit(payload)
        return attachments,page.get('nextPageToken')
