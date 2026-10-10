"""Independent supplier reply worker. Never enters customer ingestion."""
import asyncio
from app.integrations.email.supplier_gmail import SupplierGmailProvider
from app.workers.supplier_replies import SupplierReplyPoller


class SupplierEmailWorker:
    def __init__(self, provider, *, catalog=None, sessions=None, poller=None):
        self.provider = provider
        self.poller = poller or SupplierReplyPoller(provider)
        self.price_page = None
        self.catalog = catalog
        self.sessions = sessions

    @classmethod
    async def create(cls):
        provider = SupplierGmailProvider()
        provider.service = await asyncio.to_thread(provider._build_service)
        return cls(provider)

    async def run_once(self):
        # Check identity/auth even when there are no live purchase threads yet.
        await asyncio.to_thread(self.provider.readonly_smoke_check, 1)
        count = await self.poller.run_once()
        from app.services.supplier_price_import import import_attachment
        from app.database.session import async_session
        from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway
        from app.integrations.moysklad.client import MoySkladClient
        attachments, next_page = await asyncio.to_thread(self.provider.price_attachments, self.price_page)
        failed = 0
        if attachments:
            products = await self.catalog() if self.catalog else await AsyncMoySkladGateway(MoySkladClient()).get_products()
            for attachment in attachments:
                try:
                    async with (self.sessions or async_session)() as session, session.begin():
                        await import_attachment(session, **attachment, products=products)
                    count += 1
                except ValueError:
                    # Unknown sender/layout is not a reason to starve other suppliers.
                    # Next inbox sweep can retry after manager configuration changes.
                    failed += 1
        self.price_page = next_page
        return dict(received=count+failed, processed=count, failed=failed)


if __name__ == '__main__':
    from app.workers.email_runtime import main
    main('supplier')
