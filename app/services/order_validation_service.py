from app.services.product_service import ProductService
from app.schemas.order import OrderCreate


class OrderValidationError(Exception):
    pass


class OrderValidationService:
    def __init__(self) -> None:
        self.product_service = ProductService()

    def validate(self, order: OrderCreate) -> dict:
        catalog = self.product_service.get_catalog()

        products_by_id = {
            product["id"]: product
            for product in catalog
        }

        validated_items = []
        total = 0

        for item in order.items:
            product = products_by_id.get(item.id)

            if not product:
                raise OrderValidationError(
                    f"Товар {item.id} не найден в МойСклад"
                )

            available = product.get("total_available", 0) or 0

            if available < item.qty:
                raise OrderValidationError(
                    f"Недостаточно товара '{product['name']}'. "
                    f"Запрошено: {item.qty}, доступно: {available}"
                )

            price = product.get("price")

            if price is None:
                raise OrderValidationError(
                    f"У товара '{product['name']}' не указана цена"
                )

            item_total = price * item.qty
            total += item_total

            validated_items.append(
                {
                    "id": product["id"],
                    "name": product["name"],
                    "article": product.get("article"),
                    "price": price,
                    "qty": item.qty,
                    "available": available,
                    "sum": item_total,
                }
            )

        return {
            "customer_name": order.customer_name,
            "phone": order.phone,
            "telegram": order.telegram,
            "comment": order.comment,
            "items": validated_items,
            "total": total,
        }