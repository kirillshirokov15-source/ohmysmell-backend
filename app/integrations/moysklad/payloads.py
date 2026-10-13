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


def reservation(organization_id, counterparty_id, warehouse_id, items, operation_key):
    payload = customerorder(organization_id, counterparty_id, items, operation_key=operation_key)
    payload['store'] = reference('store', warehouse_id)
    for position in payload['positions']:
        position['reserve'] = position['quantity']
    return payload


def supply(organization_id, supplier_id, warehouse_id, items, operation_key, purchase_order_id=None):
    """Receipt costs are purchase costs, never the customer's retail prices."""
    payload = customerorder(organization_id, supplier_id, items, operation_key=operation_key)
    payload['store'] = reference('store', warehouse_id)
    payload['applicable'] = True
    if purchase_order_id:
        payload['purchaseOrder'] = reference('purchaseorder', purchase_order_id)
    return payload


def stock_enter(organization_id, warehouse_id, items, operation_key):
    """Inventory adjustment into OUR X warehouse, NOT supplier receipt.

    Source of truth for quantities and merchandise identity is the verified
    MoySklad product ID. Commercial site retail price belongs ONLY to demand.
    An inventory valuation must not be fabricated from customer retail price.
    MoySklad may apply its default valuation when the price is omitted;
    until verified against that account, production auto-posting stays gated.
    """
    positions = []
    for item in items:
        if not isinstance(item, dict) or type(item.get("qty")) is not int or item["qty"] <= 0:
            raise ValueError("Оприходование: количество должно быть положительным целым")
        if not item.get("id"):
            raise ValueError("Оприходование: требуется сопоставленный товар МойСклад")
        position = {
            "quantity": item["qty"],
            "assortment": reference("product", item["id"]),
        }
        positions.append(position)
    if not positions:
        raise ValueError("Оприходование: нет товаров")
    return {
        "organization": reference("organization", organization_id),
        "store": reference("store", warehouse_id),
        "positions": positions,
        "applicable": True,
        "description": "OhMySmell: внутренний склад Х. Не закупка у контрагента.",
        "externalCode": operation_key,
        "syncId": str(uuid5(NAMESPACE_URL, "ohmysmell:" + operation_key)),
    }


def direct_demand(organization_id, buyer_id, warehouse_id, items, operation_key):
    """Direct goods shipment from X; no supplier counterparty or purchase order.

    Unlike a reservation flow, a demand needs a real buyer counterparty ID.
    """
    payload = customerorder(organization_id, buyer_id, items, operation_key=operation_key)
    payload["store"] = reference("store", warehouse_id)
    payload["applicable"] = True
    return payload
