"""Independent supplier reply worker. Never enters customer ingestion."""
import asyncio
from app.integrations.email.supplier_gmail import SupplierGmailProvider
from app.workers.supplier_replies import SupplierReplyPoller


class SupplierEmailWorker:
    def __init__(self, provider):
        self.provider = provider
        self.poller = SupplierReplyPoller(provider)

    @classmethod
    async def create(cls):
        provider = SupplierGmailProvider()
        provider.service = await asyncio.to_thread(provider._build_service)
        return cls(provider)

    async def run_once(self):
        # Check identity/auth even when there are no live purchase threads yet.
        await asyncio.to_thread(self.provider.readonly_smoke_check, 1)
        count = await self.poller.run_once()
        return dict(received=count, processed=count, failed=0)


if __name__ == '__main__':
    from app.workers.email_runtime import main
    main('supplier')
