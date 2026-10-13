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



def inventory_enter(organization_id, warehouse_id, items, operation_key):
    """Оприходование OUR physical stock: /entity/enter, never supplier receipt.

    Valuation is a separate accounting policy. Caller must supply an
    explicitly configured unit cost; the Tilda selling price is not valid.
    """
    payload = {
        "organization": reference("organization", organization_id),
        "store": reference("store", warehouse_id),
        "positions": [],
        "description": "OhMySmell · собственный склад Х · учёт физического остатка · " + operation_key,
        "applicable": True,
        "externalCode": operation_key,
        "syncId": str(uuid5(NAMESPACE_URL, "ohmysmell:" + operation_key)),
    }
    for item in items:
        if (type(item.get("qty")) is not int or item["qty"] <= 0
                or type(item.get("price")) is not int or item["price"] < 0):
            raise ValueError("Оприходование требует точное количество и учётную стоимость в копейках.")
        payload["positions"].append({
            "assortment": reference("product", item["id"]),
            "quantity": item["qty"],
            "price": item["price"],
        })
    if not payload["positions"]:
        raise ValueError("Пустое оприходование запрещено.")
    return payload


def direct_demand(organization_id, counterparty_id, warehouse_id, items, operation_key, *, order_id):
    """Отгрузка directly from own warehouse X, no fictitious vendor/reserve.

    Every position uses the immutable customer sale price. MoySklad requires
    an actual buyer counterparty even when stock came from our own warehouse.
    """
    if type(order_id) is not int or order_id <= 0:
        raise ValueError("Для отгрузки нужен связанный заказ CRM.")
    payload = customerorder(organization_id, counterparty_id, items,
        description=f"OhMySmell · заказ CRM №{order_id} · товары со склада Х",
        operation_key=operation_key)
    payload["store"] = reference("store", warehouse_id)
    payload["applicable"] = True
    return payload
