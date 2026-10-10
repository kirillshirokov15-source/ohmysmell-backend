"""Supplier inbox fixtures: never initializes OAuth or a network transport."""
from copy import deepcopy


class FakeSupplierGmailProvider:
    mailbox_role='supplier'
    account='supplier@example.invalid'
    def __init__(self, attachments=()):
        self.attachments=list(attachments)
    def readonly_smoke_check(self,max_messages=5):
        return {'account':self.account,'messages_found':len(self.attachments),'email_sent':False}
    def price_attachments(self,page_token=None):
        return deepcopy(self.attachments),None
