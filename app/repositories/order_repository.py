from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.order import Order, OrderItem
from app.services.order_lifecycle import OrderStatus, ensure_order_transition


async def create_order(validated_order: dict) -> Order:
    async with async_session() as session:
        key = validated_order.get("_request_key")
        if key:
            from app.services.checkout_service import transaction_lock, CheckoutConflict
            await transaction_lock(session, "direct-order:" + key)
            existing = (await session.execute(select(Order).where(Order.request_key == key)
                .options(selectinload(Order.items)))).scalar_one_or_none()
            if existing:
                if existing.request_hash != validated_order["_request_hash"]:
                    raise CheckoutConflict("Ключ запроса уже использован")
                return existing
        order = Order(
            request_key=key, request_hash=validated_order.get("_request_hash"),
            customer_name=validated_order["customer_name"],
            phone=validated_order["phone"],
            customer_id=validated_order.get("customer_id"),
            customer_type=validated_order["customer_type"],
            source=validated_order["source"],
            counterparty_id=validated_order.get("counterparty_id"),
            telegram=validated_order.get("telegram"),
            comment=validated_order.get("comment"),
            status=OrderStatus.NEW,
            total=validated_order["total"],
        )

        for item in validated_order["items"]:
            order.items.append(
                OrderItem(
                    product_id=item["id"],
                    name=item["name"],
                    article=item.get("article"),
                    price=item["price"],
                    qty=item["qty"],
                    item_total=item["sum"],
                )
            )

        session.add(order)

        await session.commit()
        await session.refresh(order, attribute_names=["customer_email"])
        return order


async def get_order(order_id: int) -> Order | None:
    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )

        return result.scalar_one_or_none()


async def list_orders(limit: int = 50, offset: int = 0, category: str | None = None) -> list[Order]:
    async with async_session() as session:
        query = select(Order).options(selectinload(Order.items))
        if category in {"new", "assembling", "assembled", "shipped", "cancelled"}:
            query = query.where(Order.fulfillment_status == category)
        elif category in {"paid", "unpaid"}:
            query = query.where(Order.payment_status == category, Order.fulfillment_status != "cancelled")
        elif category == "review":
            query = query.where(Order.needs_review.is_(True))
        result = await session.execute(query.order_by(Order.id.desc())
            .limit(min(max(limit, 1), 100)).offset(max(offset, 0)))
        return list(result.scalars().unique().all())


async def set_order_counterparty(
    order_id: int,
    counterparty_id: str,
    counterparty_name: str,
) -> Order | None:
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(
                Order.id == order_id
            )
        )

        order = result.scalar_one_or_none()

        if order is None:
            return None

        order.counterparty_id = counterparty_id
        order.counterparty_name = counterparty_name

        await session.commit()
        return order

async def set_moysklad_order(
    order_id: int,
    moysklad_order_id: str,
    moysklad_order_name: str,
) -> Order | None:
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.id == order_id)
        )

        order = result.scalar_one_or_none()

        if order is None:
            return None

        order.moysklad_order_id = moysklad_order_id
        order.moysklad_order_name = moysklad_order_name
        ensure_order_transition(order.status, OrderStatus.CREATED_IN_MOYSKLAD)
        order.status = OrderStatus.CREATED_IN_MOYSKLAD

        await session.commit()
        await session.refresh(order)

        return order


async def get_order_by_request_key(key):
    async with async_session() as session:
        return (await session.execute(select(Order).where(Order.request_key == key)
            .options(selectinload(Order.items)))).scalar_one_or_none()
