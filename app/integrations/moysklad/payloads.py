"""Pure payload builders. Calling them cannot perform an external write."""
from uuid import NAMESPACE_URL, uuid5

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


def reference(entity: str, identifier: str):
    import re
    if not identifier or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Некорректный идентификатор МойСклад")
    return {"meta": {"href": f"{BASE_URL}/entity/{entity}/{identifier}",
                     "type": entity, "mediaType": "application/json"}}


def customerorder(organization_id, counterparty_id, items, description=None, operation_key=None):
    positions = []
    for item in items:
        if type(item["price"]) is not int or item["price"] < 0 or type(item["qty"]) is not int or item["qty"] <= 0:
            raise ValueError("Цена и количество должны быть целыми и допустимыми")
        positions.append({"quantity": item["qty"], "price": item["price"],
                          "assortment": reference("product", item["id"])})
    if not positions:
        raise ValueError("Заказ не содержит позиций")
    payload = {"organization": reference("organization", organization_id),
               "agent": reference("counterparty", counterparty_id), "positions": positions}
    if description:
        payload["description"] = description
    if operation_key:
        payload["externalCode"] = operation_key
        payload["syncId"] = str(uuid5(NAMESPACE_URL, "ohmysmell:" + operation_key))
    return payload


def demand(organization_id, counterparty_id, warehouse_id, customer_order_id, items, operation_key):
    payload = customerorder(organization_id, counterparty_id, items, operation_key=operation_key)
    payload["store"] = reference("store", warehouse_id)
    payload["customerOrder"] = reference("customerorder", customer_order_id)
    return payload
