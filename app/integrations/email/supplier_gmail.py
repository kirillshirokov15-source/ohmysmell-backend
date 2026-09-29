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
