from dataclasses import dataclass

from app.config.settings import settings
from app.models.sales import CustomerType


class PriceConfigurationError(Exception):
    pass


class PriceNotConfiguredError(Exception):
    pass


@dataclass(frozen=True)
class ProductPrice:
    amount_minor: int
    price_type: str

    @property
    def value(self) -> int:
        """Backward-compatible alias; value is now always minor units."""
        return self.amount_minor


class PriceService:
    """Selects MoySklad sale prices by configured price type, never by position."""

    def __init__(self, price_type_mapping: dict[CustomerType, str] | None = None):
        self.price_type_mapping = (
            price_type_mapping
            if price_type_mapping is not None
            else {
                CustomerType.WHOLESALE: settings.moysklad_wholesale_price_type,
                CustomerType.RETAIL: settings.moysklad_retail_price_type,
            }
        )

    @staticmethod
    def _price_type_name(sale_price: dict) -> str | None:
        price_type = sale_price.get("priceType") or {}
        return price_type.get("name")

    def available_price_types(self, product: dict) -> list[str]:
        return [
            name
            for sale_price in product.get("salePrices") or []
            if (name := self._price_type_name(sale_price))
        ]

    def get_price(
        self,
        product: dict,
        customer_type: CustomerType,
    ) -> ProductPrice:
        configured_type = self.price_type_mapping.get(customer_type)

        if not configured_type:
            raise PriceConfigurationError(
                f"Тип цены МойСклад для customer_type={customer_type.value} не настроен"
            )

        for sale_price in product.get("salePrices") or []:
            if self._price_type_name(sale_price) != configured_type:
                continue

            value = sale_price.get("value")
            if value is None or isinstance(value, bool) or not isinstance(value, int):
                break

            return ProductPrice(
                amount_minor=value,
                price_type=configured_type,
            )

        raise PriceNotConfiguredError(
            f"Для товара '{product.get('name')}' не настроена цена "
            f"типа '{configured_type}'"
        )
