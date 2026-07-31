from app.integrations.moysklad.client import MoySkladClient


class ProductService:
    def __init__(self) -> None:
        self.client = MoySkladClient()

    @staticmethod
    def _extract_id_from_href(href: str | None) -> str | None:
        if not href:
            return None

        clean_href = href.split("?")[0]
        return clean_href.rstrip("/").split("/")[-1]

    @staticmethod
    def _get_price(product: dict) -> float | None:
        sale_prices = product.get("salePrices") or []

        if not sale_prices:
            return None

        value = sale_prices[0].get("value")

        if value is None:
            return None

        return value / 100

    def get_catalog(self) -> list[dict]:
        products = self.client.get_products()
        stores = self.client.get_stores()
        stock_report = self.client.get_stock_by_store()

        active_stores = {
            store["id"]: {
                "id": store["id"],
                "name": store.get("name"),
                "stock": 0,
                "reserve": 0,
                "available": 0,
                "in_transit": 0,
            }
            for store in stores
            if not store.get("archived", False)
        }

        stocks_by_product: dict[str, dict] = {}

        for stock_row in stock_report.get("rows", []):
            product_href = stock_row.get("meta", {}).get("href")
            product_id = self._extract_id_from_href(product_href)

            if not product_id:
                continue

            product_stores = {
                store_id: store_data.copy()
                for store_id, store_data in active_stores.items()
            }

            for store_stock in stock_row.get("stockByStore", []):
                store_href = store_stock.get("meta", {}).get("href")
                store_id = self._extract_id_from_href(store_href)

                if not store_id:
                    continue

                stock = store_stock.get("stock", 0) or 0
                reserve = store_stock.get("reserve", 0) or 0
                in_transit = store_stock.get("inTransit", 0) or 0

                product_stores[store_id] = {
                    "id": store_id,
                    "name": store_stock.get("name"),
                    "stock": stock,
                    "reserve": reserve,
                    "available": max(stock - reserve, 0),
                    "in_transit": in_transit,
                }

            stocks_by_product[product_id] = product_stores

        catalog = []

        for product in products:
            if product.get("archived", False):
                continue

            product_id = product.get("id")

            product_stores = stocks_by_product.get(
                product_id,
                {
                    store_id: store_data.copy()
                    for store_id, store_data in active_stores.items()
                },
            )

            total_stock = sum(
                store["stock"]
                for store in product_stores.values()
            )

            total_reserve = sum(
                store["reserve"]
                for store in product_stores.values()
            )

            total_available = sum(
                store["available"]
                for store in product_stores.values()
            )

            images = product.get("images") or {}
            image_meta = images.get("meta") or {}

            catalog.append(
                {
                    "id": product_id,
                    "name": product.get("name"),
                    "article": product.get("article"),
                    "code": product.get("code"),
                    "description": product.get("description"),
                    "category": product.get("pathName"),
                    "price": self._get_price(product),
                    "image_count": image_meta.get("size", 0),
                    "stocks": list(product_stores.values()),
                    "total_stock": total_stock,
                    "total_reserve": total_reserve,
                    "total_available": total_available,
                    "available": total_available > 0,
                }
            )

        return catalog