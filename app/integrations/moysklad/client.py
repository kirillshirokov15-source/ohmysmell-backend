import os

import requests
from dotenv import load_dotenv


load_dotenv()

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")


class MoySkladClient:
    def __init__(self) -> None:
        if not MOYSKLAD_TOKEN:
            raise RuntimeError("MOYSKLAD_TOKEN не найден в .env")

        self.base_url = BASE_URL

        self.headers = {
            "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "Accept": "application/json;charset=utf-8",
        }

    # ---------------------------------------------------------
    # ТЕКУЩИЙ ПОЛЬЗОВАТЕЛЬ
    # ---------------------------------------------------------

    def get_current_user(self) -> dict:
        response = requests.get(
            f"{self.base_url}/context/employee",
            headers=self.headers,
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    # ---------------------------------------------------------
    # ТОВАРЫ
    # ---------------------------------------------------------

    def get_products(self) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/entity/product",
            headers=self.headers,
            params={
                "limit": 1000,
            },
            timeout=60,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("rows", [])

    # ---------------------------------------------------------
    # ФОТОГРАФИИ ТОВАРА
    # ---------------------------------------------------------

    def get_product_images(self, product_id: str) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/entity/product/{product_id}/images",
            headers=self.headers,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("rows", [])

    # ---------------------------------------------------------
    # СКЛАДЫ
    # ---------------------------------------------------------

    def get_stores(self) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/entity/store",
            headers=self.headers,
            params={
                "limit": 100,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("rows", [])

    # ---------------------------------------------------------
    # ОСТАТКИ ПО СКЛАДАМ
    # ---------------------------------------------------------

    def get_stock_by_store(self) -> dict:
        response = requests.get(
            f"{self.base_url}/report/stock/bystore",
            headers=self.headers,
            timeout=60,
        )

        response.raise_for_status()

        return response.json()

    # ---------------------------------------------------------
    # ОРГАНИЗАЦИИ
    # ---------------------------------------------------------

    def get_organizations(self) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/entity/organization",
            headers=self.headers,
            timeout=30,
        )

        if not response.ok:
            raise RuntimeError(
                f"MoySklad error {response.status_code}: "
                f"{response.text}"
            )

        data = response.json()

        return data.get("rows", [])

    # ---------------------------------------------------------
    # ПОИСК КОНТРАГЕНТОВ
    # ---------------------------------------------------------

    def search_counterparties(self, query: str) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/entity/counterparty",
            headers=self.headers,
            params={
                "search": query,
                "limit": 20,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("rows", [])

    # ---------------------------------------------------------
    # ПОЛУЧИТЬ КОНТРАГЕНТА ПО ID
    # ---------------------------------------------------------

    def get_counterparty(self, counterparty_id: str) -> dict:
        response = requests.get(
            (
                f"{self.base_url}/entity/counterparty/"
                f"{counterparty_id}"
            ),
            headers=self.headers,
            timeout=30,
        )

        response.raise_for_status()

        return response.json()

    # ---------------------------------------------------------
    # СОЗДАТЬ ЗАКАЗ ПОКУПАТЕЛЯ
    # ---------------------------------------------------------

    def create_customer_order(
        self,
        organization_id: str,
        counterparty_id: str,
        items: list[dict],
        description: str | None = None,
    ) -> dict:
        positions = []

        for item in items:
            positions.append(
                {
                    "quantity": item["qty"],
                    "price": item["price"],
                    "assortment": {
                        "meta": {
                            "href": (
                                f"{self.base_url}/entity/product/"
                                f"{item['id']}"
                            ),
                            "type": "product",
                            "mediaType": "application/json",
                        }
                    },
                }
            )

        payload = {
            "organization": {
                "meta": {
                    "href": (
                        f"{self.base_url}/entity/organization/"
                        f"{organization_id}"
                    ),
                    "type": "organization",
                    "mediaType": "application/json",
                }
            },
            "agent": {
                "meta": {
                    "href": (
                        f"{self.base_url}/entity/counterparty/"
                        f"{counterparty_id}"
                    ),
                    "type": "counterparty",
                    "mediaType": "application/json",
                }
            },
            "positions": positions,
        }

        if description:
            payload["description"] = description

        response = requests.post(
            f"{self.base_url}/entity/customerorder",
            headers=self.headers,
            json=payload,
            timeout=60,
        )

        response.raise_for_status()

        return response.json()
