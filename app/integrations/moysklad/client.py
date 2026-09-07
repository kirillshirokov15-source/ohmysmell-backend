import os
import re
import requests
from dotenv import load_dotenv
from app.integrations.http_tls import verified_session
from app.integrations.write_guard import require_external_writes
from app.integrations.moysklad.payloads import BASE_URL, customerorder

load_dotenv()
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN")


class MoySkladClient:
    def __init__(self, session: requests.Session | None = None):
        if not MOYSKLAD_TOKEN:
            raise RuntimeError("MOYSKLAD_TOKEN is not configured")
        self.base_url = BASE_URL
        self.session = session or verified_session()
        self.headers = {"Authorization": f"Bearer {MOYSKLAD_TOKEN}",
            "Accept-Encoding": "gzip", "Content-Type": "application/json",
            "Accept": "application/json;charset=utf-8"}

    def _get(self, path, params=None):
        response = self.session.get(f"{self.base_url}/{path}", headers=self.headers,
                                    params=params, timeout=(5, 30))
        response.raise_for_status()
        return response.json()

    def _rows(self, path, params=None):
        offset, limit, result = 0, 1000, []
        previous = None
        while True:
            data = self._get(path, {**(params or {}), "limit": limit, "offset": offset})
            rows = data.get("rows", [])
            if rows and rows == previous:
                raise RuntimeError("MoySklad repeated a result page")
            result.extend(rows)
            previous = rows
            offset += len(rows)
            total = data.get("meta", {}).get("size")
            if not rows or (total is not None and offset >= total):
                break
            if total is None and len(rows) < limit:
                break
            if offset > 1_000_000:
                raise RuntimeError("MoySklad pagination safety limit exceeded")
        return result

    @staticmethod
    def _id(value):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid MoySklad identifier")
        return value

    def get_current_user(self):
        return self._get("context/employee")

    def get_products(self):
        return self._rows("entity/product")

    def get_product_images(self, product_id):
        return self._rows(f"entity/product/{self._id(product_id)}/images")

    def get_stores(self):
        return self._rows("entity/store")

    def get_stock_by_store(self):
        return {"rows": self._rows("report/stock/bystore")}

    def get_organizations(self):
        return self._rows("entity/organization")

    def get_price_types(self):
        data = self._get("context/companysettings/pricetype")
        return data if isinstance(data, list) else data.get("rows", [])

    def search_counterparties(self, query):
        return self._rows("entity/counterparty", {"search": query})

    def get_recent_counterparties(self, limit=10):
        return self._get("entity/counterparty", {"limit": min(max(limit, 1), 100),
                         "order": "updated,desc"}).get("rows", [])

    def get_counterparty(self, counterparty_id):
        return self._get(f"entity/counterparty/{self._id(counterparty_id)}")

    def create_document(self, entity, payload):
        require_external_writes()
        if entity not in {"customerorder", "demand"}:
            raise ValueError("Unsupported document entity")
        response = self.session.post(f"{self.base_url}/entity/{entity}",
            headers=self.headers, json=payload, timeout=(5, 30))
        response.raise_for_status()
        return response.json()

    def create_customer_order(self, organization_id, counterparty_id, items,
                              description=None, operation_key=None):
        require_external_writes()
        return self.create_document("customerorder", customerorder(
            organization_id, counterparty_id, items, description, operation_key))
