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

        self.headers = {
            "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    def get_current_user(self) -> dict:
        response = requests.get(
            f"{BASE_URL}/context/employee",
            headers=self.headers,
            timeout=20,
        )

        response.raise_for_status()
        return response.json()

    def get_products(self) -> list[dict]:
        response = requests.get(
            f"{BASE_URL}/entity/product",
            headers=self.headers,
            params={
                "limit": 100,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        return data.get("rows", [])