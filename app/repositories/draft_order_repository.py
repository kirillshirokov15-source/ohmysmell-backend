from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.customer import Customer
from app.models.draft_order import DraftOrder, DraftOrderItem, ProductMatchStatus
from app.models.inbound_message import InboundMessage, MessageProcessingStatus
from app.models.order import Order, OrderItem
from app.models.notification import DraftNotification
from app.models.sales import CustomerType, OrderSource, channel_customer_type, order_customer_type, customer_type_policy
from app.services.order_lifecycle import OrderStatus, ensure_order_transition, InvalidOrderTransitionError


class DuplicateInboundMessageError(Exception):
    pass


class DraftOrderRepository:
    expected_revision = None
    @staticmethod
    def _ensure_editable(draft):
        if draft.finalized_order_id or draft.status not in {"draft", "needs_review", "ready"}:
            raise InvalidOrderTransitionError("Черновик уже закрыт; обновите карточку")

    @classmethod
    def _invalidate(cls, draft):
        cls._ensure_editable(draft)
        draft.revision += 1
        draft.status = OrderStatus.NEEDS_REVIEW
        draft.total = None

    async def _locked(self, session, draft_id):
        result = await session.execute(
            self._query().where(DraftOrder.id == draft_id).with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft and self.expected_revision is not None and draft.revision != self.expected_revision:
            raise InvalidOrderTransitionError("Карточка устарела; обновите черновик")
        return draft

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

    async def list(self, limit: int = 50, offset: int = 0, active_only: bool = False, status: str | None = None) -> list[DraftOrder]:
        async with async_session() as session:
            query = self._query().order_by(DraftOrder.created_at.desc())
            if status:
                query = query.where(DraftOrder.status == status)
            if active_only:
                query = query.where(DraftOrder.status.in_(["draft", "needs_review", "ready"]))
            result = await session.execute(
                query.limit(min(max(limit, 1), 100)).offset(max(offset, 0)))
            return list(result.scalars().unique().all())

    async def create(self, data: dict) -> DraftOrder:
        async with async_session() as session:
            message = InboundMessage(
                source=data.get("source", "email"),
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
                source=data.get("source", OrderSource.EMAIL),
                status=data["status"],
                sender_email=data["sender_email"],
                customer_name=data.get("customer_name"),
                subject=data.get("subject"),
                counterparty_id=data.get("counterparty_id"),
                contact_details=data.get("contact_details", {}),
                counterparty_candidates=data.get("counterparty_candidates", []),
                total=data.get("total"),
                review_notes="\n".join(data.get("problems", [])) or None,
            )
            message.customer_id = data["customer_id"]
            session.add(message)
            try:
                await session.flush()
            except IntegrityError as error:
                await session.rollback()
                raise DuplicateInboundMessageError from error
            draft.inbound_message_id = message.id
            for item in data["items"]:
                draft.items.append(DraftOrderItem(**item))
            session.add(draft)
            try:
                await session.flush()
                session.add(DraftNotification(draft_id=draft.id))
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise DuplicateInboundMessageError from error
            return draft

    async def mark_notified(self, draft_id: int):
        from sqlalchemy import update
        async with async_session() as session:
            await session.execute(update(DraftNotification).where(
                DraftNotification.draft_id == draft_id).values(status="sent"))
            await session.commit()

    async def set_customer_type(
        self, draft_id: int, customer_type: CustomerType
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await self._locked(session, draft_id)
            if draft is None:
                return None
            if channel_customer_type(draft.source):
                raise InvalidOrderTransitionError("Тип заказа определяется каналом; обновите карточку")
            self._invalidate(draft)
            await session.execute(update(Customer).where(Customer.id == draft.customer_id)
                                  .values(customer_type=customer_type))
            draft.customer_type = customer_type
            await session.commit()
            return draft

    async def set_counterparty(
        self, draft_id: int, counterparty_id: str, counterparty_name: str
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await self._locked(session, draft_id)
            if draft is None:
                return None
            self._invalidate(draft)
            draft.counterparty_id = counterparty_id
            draft.counterparty_name = counterparty_name
            await session.execute(update(Customer).where(Customer.id == draft.customer_id)
                                  .values(moysklad_counterparty_id=counterparty_id))
            await session.commit()
            return draft

    async def set_counterparty_candidates(
        self, draft_id: int, candidates: list[dict]
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await self._locked(session, draft_id)
            if draft is None:
                return None
            self._ensure_editable(draft)
            draft.counterparty_candidates = candidates
            await session.commit()
            return draft

    async def resolve_item(
        self, draft_id: int, item_id: int, product: dict
    ) -> DraftOrder | None:
        async with async_session() as session:
            draft = await self._locked(session, draft_id)
            if draft is None:
                return None
            self._ensure_editable(draft)
            item = next((i for i in draft.items if i.id == item_id), None)
            if item is None:
                return None
            self._invalidate(draft)
            item.price = None
            item.item_total = None
            item.match_status = ProductMatchStatus.MATCHED
            item.product_id = product["id"]
            item.product_name = product.get("name")
            item.article = product.get("article")
            item.candidates = []
            await session.commit()
            return draft

    async def save_review(
        self,
        draft_id: int,
        status: OrderStatus,
        total: int | None,
        problems: list[str],
        priced_items: dict[int, tuple[int, int]],
        expected_revision: int | None = None,
    ) -> DraftOrder | None:
        async with async_session() as session:
            result = await session.execute(
                self._query().where(DraftOrder.id == draft_id).with_for_update()
            )
            draft = result.scalar_one_or_none()
            if draft is None:
                return None
            if draft.finalized_order_id:
                return draft
            self._ensure_editable(draft)
            if expected_revision is not None and draft.revision != expected_revision:
                raise InvalidOrderTransitionError("Черновик изменён параллельно; повторите действие")
            effective = order_customer_type(draft.source, draft.customer_type)
            if draft.customer_type != effective:
                profile = await session.get(Customer, draft.customer_id)
                draft.contact_details = {**(draft.contact_details or {}),
                    "customer_type_policy": customer_type_policy(draft.source, profile.customer_type)}
                draft.customer_type = effective
                draft.revision += 1
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
            return draft

    async def set_item_candidates(self, draft_id, item_id, candidates):
        async with async_session() as session, session.begin():
            draft = await self._locked(session, draft_id)
            if not draft:
                return None
            self._invalidate(draft)
            item = next((i for i in draft.items if i.id == item_id), None)
            if not item:
                raise InvalidOrderTransitionError("Позиция не найдена")
            item.candidates = candidates
            return draft

    async def reject(self, draft_id: int) -> DraftOrder | None:
        async with async_session() as session:
            draft = await self._locked(session, draft_id)
            if draft is None:
                return None
            if draft.status == OrderStatus.REJECTED:
                return draft
            self._ensure_editable(draft)
            ensure_order_transition(draft.status, OrderStatus.REJECTED)
            draft.status = OrderStatus.REJECTED
            await session.commit()
            return draft

    async def finalize(self, draft_id: int) -> Order | None:
        async with async_session() as session:
            result = await session.execute(
                self._query().where(DraftOrder.id == draft_id).with_for_update()
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
            if self.expected_revision is not None and draft.revision != self.expected_revision:
                raise InvalidOrderTransitionError("Карточка устарела; обновите черновик")
            ensure_order_transition(draft.status, OrderStatus.NEW)
            if (draft.customer_type != order_customer_type(draft.source, draft.customer_type)
                    or draft.customer_type == "unknown" or not draft.counterparty_id
                    or not draft.items or draft.total is None
                    or any(i.qty <= 0 or i.price is None or i.price < 0
                           or not i.product_id or i.match_status != "matched"
                           or i.item_total != i.price * i.qty for i in draft.items)
                    or draft.total != sum(i.item_total for i in draft.items)):
                raise InvalidOrderTransitionError("Черновик требует повторной проверки")
            order = Order(
                customer_id=draft.customer_id,
                customer_type=draft.customer_type,
                source=draft.source,
                customer_name=draft.customer_name or draft.sender_email,
                phone=(draft.contact_details or {}).get("phone", ""),
                telegram=(draft.contact_details or {}).get("telegram"),
                counterparty_id=draft.counterparty_id,
                counterparty_name=draft.counterparty_name,
                comment=(draft.contact_details or {}).get("comment") or f"{draft.source}: {draft.subject or 'без темы'}",
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
            from app.services.supply_service import capture_order_supply
            await capture_order_supply(session, order)
            draft.status = OrderStatus.NEW
            await session.execute(update(InboundMessage).where(
                InboundMessage.id == draft.inbound_message_id).values(order_id=order.id))
            await session.commit()
            await session.refresh(order, attribute_names=["customer_email"])
            return order
