"""Validation at the physical-picking boundary for Tilda customer orders."""
from collections import defaultdict


def require_complete_picking_plan(order, plans):
    """All sale units must have a warehouse before any picker confirmation.

    Unlike stock reservation this intentionally DOES NOT require X goods to
    have been posted: the picker confirms the physical items first. No supplier
    confirmation or Buying purchase can substitute for a source allocation.
    """
    from app.services.order_operations import OperationError

    if not order.manual_fulfillment:
        raise OperationError("Это действие предусмотрено только для заказов Tilda.")
    requested = {item.id: item.qty for item in order.items}
    actual = defaultdict(int)
    if not requested or not plans:
        raise OperationError("Назначьте основной склад или склад Х для каждой позиции.")
    for plan in plans:
        if plan.status != "planned":
            raise OperationError("Складской план требует проверки перед сборкой.")
        for allocation in plan.allocations:
            if allocation.supply_status == "cancelled":
                continue
            if not allocation.managed_source or allocation.source_type not in {"own", "partner_x"}:
                raise OperationError("Заказ Tilda может собираться только со своего склада и склада Х.")
            if allocation.order_item_id not in requested or allocation.qty <= 0:
                raise OperationError("Источник или количество позиции не совпадают с заказом.")
            actual[allocation.order_item_id] += allocation.qty
    if dict(actual) != requested:
        raise OperationError("Распределите все товары по двум складам до начала сборки.")
    return True
