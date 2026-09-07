"""Anonymous website intake: server prices and durable idempotent draft creation."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select, text
from app.database.session import async_session
from app.models.customer import Customer, CustomerIdentity
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.fulfillment import CheckoutRequest
from app.models.inbound_message import InboundMessage
from app.models.notification import DraftNotification
from app.models.sales import CustomerType
from app.services.product_service import ProductService
from app.services.stock_allocation import allocate
from app.config.settings import settings


class CheckoutConflict(ValueError):
    pass


def request_hash(payload) -> str:
    data = payload.model_dump(mode="json")
    # Product order and duplicate lines do not affect the logical request.
    items = {}
    for item in data["items"]:
        items[item["product_id"]] = items.get(item["product_id"], 0) + item["qty"]
    data["items"] = sorted(items.items())
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def transaction_lock(session, key: str):
    # Stable across Python processes; PostgreSQL owns release on rollback/commit.
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


class CheckoutService:
    def __init__(self, products=None):
        self.products = products or ProductService()

    async def submit(self, payload, key: str) -> dict:
        digest = request_hash(payload)
        async with async_session() as session:
            existing = await session.get(CheckoutRequest, key)
            if existing:
                return self._replay(existing, digest)
        catalog = await self.products.get_catalog_async(customer_type=CustomerType.RETAIL)
        quantities = {}
        for item in payload.items:
            quantities[item.product_id] = quantities.get(item.product_id, 0) + item.qty
        allocate([{"id": pid, "qty": qty} for pid, qty in quantities.items()],
                 catalog, settings.warehouse_ids)
        products = {p["id"]: p for p in catalog}
        async with async_session() as session, session.begin():
            await transaction_lock(session, "checkout:" + key)
            existing = await session.get(CheckoutRequest, key)
            if existing:
                return self._replay(existing, digest)
            await transaction_lock(session, "customer-email:" + payload.email)
            customer = (await session.execute(select(Customer).join(CustomerIdentity).where(
                CustomerIdentity.identity_type == "email",
                CustomerIdentity.normalized_value == payload.email,
            ))).scalar_one_or_none()
            if not customer:
                customer = Customer(customer_type="retail", display_name=payload.customer_name)
                customer.identities.append(CustomerIdentity(identity_type="email",
                    normalized_value=payload.email, original_value=payload.email))
                session.add(customer)
                await session.flush()
            # Supplied contact details identify a review request; never prove login
            # or grant a wholesale price. Do not attach new identities automatically.
            priced = customer.customer_type == "retail" and all(
                products[pid]["price"] is not None for pid in quantities)
            total = sum(products[pid]["price"] * qty for pid, qty in quantities.items()) if priced else None
            if total is not None and total > 9_223_372_036_854_775_807:
                raise CheckoutConflict("Сумма заказа превышает допустимый предел")
            inbound = InboundMessage(source="website", external_message_id="website:" + key,
                sender=payload.email, subject="Заявка с сайта", body_text=payload.model_dump_json(),
                received_at=datetime.now(timezone.utc), processing_status="processed", customer_id=customer.id)
            session.add(inbound)
            await session.flush()
            draft = DraftOrder(inbound_message_id=inbound.id, source="website",
                customer_id=customer.id, customer_type=customer.customer_type,
                sender_email=payload.email, customer_name=payload.customer_name,
                subject="Заявка с сайта", status="needs_review", total=total,
                contact_details={"phone": payload.phone, "comment": payload.comment},
                counterparty_id=customer.moysklad_counterparty_id, counterparty_candidates=[],
                review_notes="Подтвердите контактные данные и условия заказа с клиентом")
            for pid, qty in quantities.items():
                product = products[pid]
                price = product["price"] if priced else None
                draft.items.append(DraftOrderItem(raw_product_text=product["name"], qty=qty,
                    match_status="matched", product_id=pid, product_name=product["name"],
                    article=product.get("article"), price=price,
                    item_total=price * qty if price is not None else None, candidates=[]))
            session.add(draft)
            await session.flush()
            response = {"request_id": key, "status": "needs_review", "currency": "RUB",
                "total_minor": total, "message": "Заявка сохранена. Менеджер подтвердит цену и наличие."}
            session.add(CheckoutRequest(key=key, request_hash=digest, draft_id=draft.id, response=response))
            session.add(DraftNotification(draft_id=draft.id))
            return response

    @staticmethod
    def _replay(existing, digest):
        if existing.request_hash != digest:
            raise CheckoutConflict("Idempotency-Key уже использован для другого запроса")
        return existing.response
