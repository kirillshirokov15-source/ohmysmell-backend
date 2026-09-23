"""Safe procurement adapters; fake references cannot be mistaken for live IDs."""
from dataclasses import dataclass
from typing import Protocol
from app.config.settings import settings


@dataclass(frozen=True)
class SentEmail:
    message_id: str
    thread_id: str
    simulated: bool


class SupplierEmailSender(Protocol):
    async def send(self, *, key: str, recipient: str, body: str) -> SentEmail: ...


class FakeSupplierEmailSender:
    async def send(self, *, key, recipient, body):
        return SentEmail("fake-message:" + key, "fake-thread:" + key, True)


class DisabledGmailSupplierSender:
    async def send(self, *, key, recipient, body):
        if not settings.external_writes_enabled or not settings.supplier_email_send_enabled:
            raise ValueError("Supplier email sending is disabled")
        # A live implementation must reconcile uncertain outcomes before retrying.
        raise ValueError("Separate supplier OAuth and reconciliation adapter required")


class ProcurementAdapter(Protocol):
    async def order(self, purchase_id: int, supplier_id: int) -> dict: ...
    async def receipt(self, purchase_id: int) -> dict: ...


class FakeProcurementAdapter:
    async def order(self, purchase_id, supplier_id):
        return {"counterparty": f"fake-supplier:{supplier_id}", "supplier_order": f"fake-order:{purchase_id}"}

    async def receipt(self, purchase_id):
        return {"receipt": f"fake-receipt:{purchase_id}", "warehouse": "Внешние поставщики"}


class DisabledMoySkladProcurementAdapter:
    async def order(self, purchase_id, supplier_id):
        self.check()

    async def receipt(self, purchase_id):
        self.check()

    @staticmethod
    def check():
        if not settings.external_writes_enabled:
            raise ValueError("External writes are disabled")
        raise ValueError("Configure verified supplier group, warehouse and reconciliation adapter before live writes")
