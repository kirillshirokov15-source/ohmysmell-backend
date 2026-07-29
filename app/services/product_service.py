from app.integrations.moysklad.client import MoySkladClient


class ProductService:
    def __init__(self) -> None:
        self.client = MoySkladClient()

    def get_catalog(self) -> list[dict]:
        raw_products = self.client.get_products()

        if not isinstance(raw_products, list):
            raise TypeError(
                f"Ожидался список товаров, получено: {type(raw_products).__name__}"
            )

        catalog: list[dict] = []

        for product in raw_products:
            if not isinstance(product, dict):
                continue

            if product.get("archived", False):
                continue

            sale_prices = product.get("salePrices") or []
            price = None

            if sale_prices:
                price_in_kopecks = sale_prices[0].get("value")

                if price_in_kopecks is not None:
                    price = price_in_kopecks / 100

            images = product.get("images") or {}
            image_meta = images.get("meta") or {}

            catalog.append(
                {
                    "id": product.get("id"),
                    "name": product.get("name"),
                    "article": product.get("article"),
                    "code": product.get("code"),
                    "description": product.get("description"),
                    "category": product.get("pathName"),
                    "price": price,
                    "image_count": image_meta.get("size", 0),
                }
            )

        return catalog