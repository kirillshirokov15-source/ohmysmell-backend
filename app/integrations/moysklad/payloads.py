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



def purchaseorder(organization_id, supplier_id, warehouse_id, items, operation_key):
    """Supplier order for Hatuna: the separate, estimated acquisition prices.

    A purchase order alone never updates stock. One idempotent document groups
    all Hatuna items for a single destination warehouse and client order.
    """
    payload = customerorder(organization_id, supplier_id, items, operation_key=operation_key)
    payload["store"] = reference("store", warehouse_id)
    payload["applicable"] = True
    payload["description"] = (
        "Внутренний учёт закупки Хатуна; расчётная себестоимость до ежемесячной сверки"
    )
    return payload


def supply(organization_id, supplier_id, warehouse_id, items, operation_key, purchase_order_id=None):
    """Receipt costs are purchase costs, never the customer's retail prices."""
    payload = customerorder(organization_id, supplier_id, items, operation_key=operation_key)
    payload['store'] = reference('store', warehouse_id)
    payload['applicable'] = True
    if purchase_order_id:
        payload['purchaseOrder'] = reference('purchaseorder', purchase_order_id)
    return payload


def enter(organization_id, warehouse_id, items, operation_key):
    """Own physical warehouse X → stock in MoySklad, no supplier or agent.

    Retail unit price is NOT inventory acquisition cost. We deliberately do
    not invent an accounting valuation; provider defaults must be confirmed
    before enabling production writes.
    """
    if not items:
        raise ValueError("Оприходование без товаров запрещено")
    rows = []
    for item in items:
        if not isinstance(item.get("id"), str) or type(item.get("qty")) is not int or item["qty"] <= 0:
            raise ValueError("Позиция оприходования не содержит подтверждённый товар и количество")
        rows.append({"quantity": item["qty"],
                     "assortment": reference("product", item["id"])})
    payload = {"organization": reference("organization", organization_id),
               "store": reference("store", warehouse_id),
               "positions": rows,
               "applicable": True,
               "description": "Оприходование собственного склада Х; цена продажи не является себестоимостью",
               "externalCode": operation_key,
               "syncId": str(uuid5(NAMESPACE_URL, "ohmysmell:" + operation_key))}
    return payload
