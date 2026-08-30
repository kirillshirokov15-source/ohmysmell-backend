from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.order import Order, OrderItem
from app.services.order_lifecycle import OrderStatus, ensure_order_transition


async def create_order(validated_order: dict) -> Order:
    async with async_session() as session:
        order = Order(
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
        await session.refresh(order)

        return order


async def get_order(order_id: int) -> Order | None:
    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )

        return result.scalar_one_or_none()


async def list_orders() -> list[Order]:
    async with async_session() as session:
        result = await session.execute(
            select(Order)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
        )
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
        await session.refresh(order)

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
