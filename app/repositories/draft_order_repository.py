from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.customer import Customer
from app.models.draft_order import DraftOrder, DraftOrderItem, ProductMatchStatus
from app.models.inbound_message import InboundMessage, MessageProcessingStatus
from app.models.order import Order, OrderItem
from app.models.sales import CustomerType, OrderSource
from app.services.order_lifecycle import OrderStatus, ensure_order_transition


class DuplicateInboundMessageError(Exception):
    pass


class DraftOrderRepository:
    @staticmethod
    def _query():
        return select(DraftOrder).options(selectinload(DraftOrder.items))

    async def get_by_external_message_id(self, external_id: str) -> DraftOrder | None:
        async with async_session() as session:
            result = await session.execute(
                self._query()
                .join(InboundMessage)
                .where(InboundMessage.external_message_id == external_id)
            )
            return result.scalar_one_or_none()

    async def get(self, draft_id: int) -> DraftOrder | None:
        async with async_session() as session:
            result = await session.execute(
                self._query().where(DraftOrder.id == draft_id)
            )
            return result.scalar_one_or_none()

    async def list(self) -> list[DraftOrder]:
        async with async_session() as session:
            result = await session.execute(
                self._query().order_by(DraftOrder.created_at.desc())
            )
            return list(result.scalars().unique().all())

    async def create(self, data: dict) -> DraftOrder:
        async with async_session() as session:
            message = InboundMessage(
                source="email",
                external_message_id=data["external_message_id"],
                sender=data["sender_email"],
                subject=data.get("subject"),
                body_text=data["body_text"],
                received_at=data["received_at"],
                processing_status=MessageProcessingStatus.PROCESSED,
                customer_id=data["customer_id"],
            )
            draft = DraftOrder(
                customer_id=data["customer_id"],
                customer_type=data["customer_type"],
                source=OrderSource.EMAIL,
                status=data["status"],
                sender_email=data["sender_email"],
                customer_name=data.get("customer_name"),
                subject=data.get("subject"),
                counterparty_id=data.get("counterparty_id"),
                counterparty_candidates=data.get("counterparty_candidates", []),
                total=data.get("total"),
                review_notes="\n".join(data.get("problems", [])) or None,
            )
            message.customer_id = data["customer_id"]
            session.add(message)
            await session.flush()
            draft.inbound_message_id = message.id
            for item in data["items"]:
                draft.items.append(DraftOrderItem(**item))
            session.add(draft)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise DuplicateInboundMessageError from error
            await session.refresh(draft)
            result = await session.execute(
                self._query().where(DraftOrder.id == draft.id)
            )
            return result.scalar_one()

    async def set_customer_type(
        self, draft_id: int, customer_type: CustomerType
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await session.get(DraftOrder, draft_id)
            if draft is None:
                return None
            customer = await session.get(Customer, draft.customer_id)
            if customer is not None:
                customer.customer_type = customer_type
            draft.customer_type = customer_type
            await session.commit()
        return await self.get(draft_id)

    async def set_counterparty(
        self, draft_id: int, counterparty_id: str, counterparty_name: str
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await session.get(DraftOrder, draft_id)
            if draft is None:
                return None
            draft.counterparty_id = counterparty_id
            draft.counterparty_name = counterparty_name
            customer = await session.get(Customer, draft.customer_id)
            if customer is not None:
                customer.moysklad_counterparty_id = counterparty_id
            await session.commit()
        return await self.get(draft_id)

    async def set_counterparty_candidates(
        self, draft_id: int, candidates: list[dict]
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await session.get(DraftOrder, draft_id)
            if draft is None:
                return None
            draft.counterparty_candidates = candidates
            await session.commit()
        return await self.get(draft_id)

    async def resolve_item(
        self, draft_id: int, item_id: int, product: dict
    ) -> DraftOrder | None:
        async with async_session() as session:
            result = await session.execute(
                select(DraftOrderItem).where(
                    DraftOrderItem.id == item_id,
                    DraftOrderItem.draft_order_id == draft_id,
                )
            )
            item = result.scalar_one_or_none()
            if item is None:
                return None
            item.match_status = ProductMatchStatus.MATCHED
            item.product_id = product["id"]
            item.product_name = product.get("name")
            item.article = product.get("article")
            item.candidates = []
            await session.commit()
        return await self.get(draft_id)

    async def save_review(
        self,
        draft_id: int,
        status: OrderStatus,
        total: int | None,
        problems: list[str],
        priced_items: dict[int, tuple[int, int]],
    ) -> DraftOrder | None:
        async with async_session() as session:
            result = await session.execute(
                self._query().where(DraftOrder.id == draft_id)
            )
            draft = result.scalar_one_or_none()
            if draft is None:
                return None
            if draft.status != status:
                ensure_order_transition(draft.status, status)
            draft.status = status
            draft.total = total
            draft.review_notes = "\n".join(problems) or None
            for item in draft.items:
                pricing = priced_items.get(item.id)
                item.price = pricing[0] if pricing else None
                item.item_total = pricing[1] if pricing else None
            await session.commit()
        return await self.get(draft_id)

    async def reject(self, draft_id: int) -> DraftOrder | None:
        async with async_session() as session:
            draft = await session.get(DraftOrder, draft_id)
            if draft is None:
                return None
            ensure_order_transition(draft.status, OrderStatus.REJECTED)
            draft.status = OrderStatus.REJECTED
            await session.commit()
        return await self.get(draft_id)

    async def finalize(self, draft_id: int) -> Order | None:
        async with async_session() as session:
            result = await session.execute(
                self._query().where(DraftOrder.id == draft_id)
            )
            draft = result.scalar_one_or_none()
            if draft is None:
                return None
            if draft.finalized_order_id:
                order_result = await session.execute(
                    select(Order)
                    .options(selectinload(Order.items))
                    .where(Order.id == draft.finalized_order_id)
                )
                return order_result.scalar_one_or_none()
            ensure_order_transition(draft.status, OrderStatus.NEW)
            order = Order(
                customer_id=draft.customer_id,
                customer_type=draft.customer_type,
                source=OrderSource.EMAIL,
                customer_name=draft.customer_name or draft.sender_email,
                phone="",
                counterparty_id=draft.counterparty_id,
                counterparty_name=draft.counterparty_name,
                comment=f"Email: {draft.subject or 'без темы'}",
                status=OrderStatus.NEW,
                total=draft.total,
            )
            order.items.extend(
                OrderItem(
                    product_id=item.product_id,
                    name=item.product_name or item.raw_product_text,
                    article=item.article,
                    price=item.price,
                    qty=item.qty,
                    item_total=item.item_total,
                )
                for item in draft.items
            )
            session.add(order)
            await session.flush()
            draft.finalized_order_id = order.id
            draft.status = OrderStatus.NEW
            message = await session.get(InboundMessage, draft.inbound_message_id)
            if message is not None:
                message.order_id = order.id
            await session.commit()
            await session.refresh(order)
            return order
