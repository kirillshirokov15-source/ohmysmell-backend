from app.services.product_service import ProductService
from app.schemas.order import OrderCreate
from app.services.price_service import (
    PriceConfigurationError,
    PriceNotConfiguredError,
)


class OrderValidationError(Exception):
    pass


class OrderValidationService:
    def __init__(self, product_service: ProductService | None = None) -> None:
        self.product_service = product_service or ProductService()

    def validate(self, order: OrderCreate) -> dict:
        try:
            catalog = self.product_service.get_catalog(
                customer_type=order.customer_type,
                strict_pricing=True,
            )
        except (PriceConfigurationError, PriceNotConfiguredError) as error:
            raise OrderValidationError(str(error)) from error

        products_by_id = {
            product["id"]: product
            for product in catalog
        }

        validated_items = []
        total = 0

        requested_quantities: dict[str, int] = {}
        for item in order.items:
            requested_quantities[item.id] = (
                requested_quantities.get(item.id, 0) + item.qty
            )

        for product_id, qty in requested_quantities.items():
            product = products_by_id.get(product_id)

            if not product:
                raise OrderValidationError(
                    f"Товар {product_id} не найден в МойСклад"
                )

            available = product.get("total_available", 0) or 0

            if available < qty:
                raise OrderValidationError(
                    f"Недостаточно товара '{product['name']}'. "
                    f"Запрошено: {qty}, доступно: {available}"
                )

            price = product.get("price")

            if price is None:
                raise OrderValidationError(
                    f"У товара '{product['name']}' не указана цена"
                )

            item_total = price * qty
            total += item_total

            validated_items.append(
                {
                    "id": product["id"],
                    "name": product["name"],
                    "article": product.get("article"),
                    "price": price,
                    "qty": qty,
                    "available": available,
                    "sum": item_total,
                }
            )

        return {
            "customer_name": order.customer_name,
            "phone": order.phone,
            "customer_type": order.customer_type,
            "source": order.source,
            "telegram": order.telegram,
            "comment": order.comment,
            "items": validated_items,
            "total": total,
        }
