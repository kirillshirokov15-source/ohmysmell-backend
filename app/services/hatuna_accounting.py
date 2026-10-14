"""Pure Hatuna purchase/receipt planning; no MoySklad or database calls.

CRM must only execute these intents AFTER trusted physical-picking confirmation.
A single X warehouse plan groups many Tilda lines into one supplier order
and one linked receipt. The existing external-operation journal executes each
step exactly once, and reserves/shipments are separate per physical store.
"""
from dataclasses import dataclass
from uuid import UUID

from app.integrations.moysklad.payloads import purchaseorder, supply


class HatunaPreflightError(ValueError):
    """Cannot produce an accounting document from unverified source data."""


def _uuid(value, label):
    try:
        normalized = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HatunaPreflightError(f"{label}: нужен подтверждённый UUID МойСклад") from exc
    if not isinstance(value, str) or normalized != value.lower():
        raise HatunaPreflightError(f"{label}: некорректный UUID МойСклад")
    return value


def provisional_cost(sale_unit_minor):
    """Estimated Hatuna unit cost = the immutable Tilda unit price minus 1,000 RUB."""
    if type(sale_unit_minor) is not int or sale_unit_minor <= 100_000:
        raise HatunaPreflightError(
            "Розничная цена <= 1 000 ₽: отрицательная или нулевая закупочная "
            "стоимость Хатуны недопустима. Требуется проверка менеджера."
        )
    return sale_unit_minor - 100_000


@dataclass(frozen=True)
class HatunaItem:
    product_id: str
    quantity: int
    sale_unit_minor: int
    source_item_id: int

    def checked(self):
        _uuid(self.product_id, "Товар")
        if type(self.quantity) is not int or not 1 <= self.quantity <= 10_000:
            raise HatunaPreflightError("Количество в заказе Хатуне некорректно")
        if type(self.source_item_id) is not int or self.source_item_id <= 0:
            raise HatunaPreflightError("Нет исходного ID позиции клиентского заказа")
        provisional_cost(self.sale_unit_minor)
        return self


def hatuna_lines(items):
    """No automatic SKU/name guessing: unique mapped order item is mandatory."""
    if not items:
        raise HatunaPreflightError("Нечего закупать у Хатуны")
    checked = [i.checked() for i in items]
    if len({i.source_item_id for i in checked}) != len(checked):
        raise HatunaPreflightError("Повтор исходной позиции в закупке: возможен двойной приход")
    return [
        {"id": item.product_id, "qty": item.quantity,
         "price": provisional_cost(item.sale_unit_minor)}
        for item in checked
    ]


def hatuna_operation_keys(order_id, x_plan_id):
    if any(type(i) is not int or i <= 0 for i in (order_id, x_plan_id)):
        raise HatunaPreflightError("Заказ клиента и складской план должны существовать в CRM")
    prefix = f"oms:hatuna:order:{order_id}:x-plan:{x_plan_id}"
    return {"purchase": prefix + ":purchase",
            "receipt": prefix + ":receipt"}


def purchase_intent(*, order_id, x_plan_id, organization_id, hatuna_id,
                    x_store_id, items, picker_confirmed):
    """Plan accounting only if a trusted picker confirmed every X line."""
    if picker_confirmed is not True:
        raise HatunaPreflightError("Сборщик ещё не подтвердил фактическое наличие всех позиций Х")
    _uuid(organization_id, "Организация")
    _uuid(hatuna_id, "Контрагент Хатуна")
    _uuid(x_store_id, "Склад Х")
    rows = hatuna_lines(items)
    key = hatuna_operation_keys(order_id, x_plan_id)["purchase"]
    payload = purchaseorder(organization_id, hatuna_id, x_store_id, rows, key)
    return {"entity": "purchaseorder", "document": payload}


def receipt_intent(*, order_id, x_plan_id, organization_id, hatuna_id,
                   x_store_id, items, picker_confirmed, purchase_id):
    """Always connect posted purchase receipt to the already verified PO."""
    if picker_confirmed is not True:
        raise HatunaPreflightError("Сборка не подтверждена")
    _uuid(organization_id, "Организация")
    _uuid(hatuna_id, "Контрагент Хатуна")
    _uuid(x_store_id, "Склад Х")
    _uuid(purchase_id, "Созданный заказ поставщику")
    rows = hatuna_lines(items)
    key = hatuna_operation_keys(order_id, x_plan_id)["receipt"]
    payload = supply(organization_id, hatuna_id, x_store_id, rows, key,
                     purchase_order_id=purchase_id)
    payload["description"] = (
        "Приёмка Хатуна по подтверждённой сборке; "
        "расчётные цены требуют ежемесячной сверки"
    )
    return {"entity": "supply", "document": payload}
