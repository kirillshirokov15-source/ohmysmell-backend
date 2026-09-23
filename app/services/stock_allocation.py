"""Deterministic allocation proposal against a fresh MoySklad stock snapshot."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


class StockAllocationError(ValueError):
    pass


def available_units(stock, reserve) -> int:
    try:
        values = [Decimal(str(v or 0)) for v in (stock, reserve)]
        if not all(v.is_finite() for v in values):
            raise ValueError
        return int(max(values[0] - values[1], Decimal(0)).to_integral_value(rounding=ROUND_FLOOR))
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise StockAllocationError("Некорректные остатки МойСклад") from error


@dataclass(frozen=True)
class Allocation:
    product_id: str
    warehouse_id: str
    qty: int


def allocate(items: list[dict], catalog: list[dict], warehouse_ids: tuple[str, ...] = ()) -> list[Allocation]:
    products = {p["id"]: p for p in catalog}
    requested = defaultdict(int)
    for item in items:
        if type(item["qty"]) is not int or item["qty"] <= 0:
            raise StockAllocationError("Количество должно быть положительным целым")
        requested[item["id"]] += item["qty"]
    result = []
    for product_id, quantity in sorted(requested.items()):
        product = products.get(product_id)
        if not product:
            raise StockAllocationError("Товар отсутствует в активном каталоге")
        if product.get("supply_source") == "external":
            if product.get("supply_availability") == "unavailable":
                raise StockAllocationError("Товар внешних поставщиков недоступен")
            # A procurement request, never a fictitious physical allocation.
            continue
        stores = {s["id"]: s for s in product.get("stocks", [])}
        order = warehouse_ids or tuple(sorted(stores))
        for warehouse_id in dict.fromkeys(order):
            stock = stores.get(warehouse_id, {})
            amount = min(quantity, available_units(stock.get("stock"), stock.get("reserve")))
            if amount:
                result.append(Allocation(product_id, warehouse_id, amount))
                quantity -= amount
            if not quantity:
                break
        if quantity:
            raise StockAllocationError(f"Недостаточно доступного товара {product_id}")
    return result
