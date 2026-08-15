from app.database.session import async_session
from app.models.order import Order, OrderItem


async def create_order(validated_order: dict) -> Order:
    async with async_session() as session:
        order = Order(
            customer_name=validated_order["customer_name"],
            phone=validated_order["phone"],
            telegram=validated_order.get("telegram"),
            comment=validated_order.get("comment"),
            status="new",
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