"""Pure internal Hatuna/X stock-entry accounting, no external calls.

After confirmed physical picking, make ONE priced enter document per X
warehouse plan. Hatuna is an internal label, never the agent of this document.
The retail price is frozen in CRM; entry uses only estimated acquisition costs.
"""
from dataclasses import dataclass
from uuid import UUID

from app.integrations.moysklad.payloads import hatuna_enter


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
    # :enter differs from the abandoned supplier-order/supply ledger keys.
    # Do not reuse a previously posted supply operation.
    return {"receipt": f"oms:hatuna:order:{order_id}:x-plan:{x_plan_id}:enter"}


def stock_entry_intent(*, order_id, x_plan_id, organization_id,
                       x_store_id, items, picker_confirmed):
    """Produce a *single* X entry with cost only after physical picking."""
    if picker_confirmed is not True:
        raise HatunaPreflightError("Сборщик ещё не подтвердил наличие товаров склада Х")
    _uuid(organization_id, "Организация")
    _uuid(x_store_id, "Склад Х")
    rows = hatuna_lines(items)
    key = hatuna_operation_keys(order_id, x_plan_id)["receipt"]
    payload = hatuna_enter(organization_id, x_store_id, rows, key, order_id)
    return {"entity": "enter", "document": payload}


async def verify_hatuna_counterparty(counterparty_id, *, client=None):
    """Read-only MoySklad identity check, never a posting or supplier confirmation.

    The accounting counterparty must be the specific, unarchived Hatuna record;
    a valid UUID alone does not prove that the selected entity is correct.
    """
    import asyncio
    _uuid(counterparty_id, "Контрагент Хатуна")
    if client is None:
        from app.integrations.moysklad.client import MoySkladClient
        client = MoySkladClient()
    try:
        record = await asyncio.to_thread(client.get_counterparty, counterparty_id)
    except Exception as exc:
        raise HatunaPreflightError(
            "Не удалось проверить контрагента Хатуна в МойСклад"
        ) from exc
    name = (record.get("name") or "").casefold()
    if (record.get("id") != counterparty_id
            or record.get("archived") is True
            or not any(fragment in name for fragment in ("хатун", "hatun"))):
        raise HatunaPreflightError(
            "ID относится не к действующему контрагенту Хатуна; проводки остановлены"
        )
    return True
