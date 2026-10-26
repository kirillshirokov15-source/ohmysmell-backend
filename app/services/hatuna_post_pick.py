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
        raise OperationError("Это действие предусмотрено для подтверждённых заказов Tilda и Gmail.")
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
                raise OperationError("Заказ может собираться только со своего склада и склада Х.")
            if allocation.order_item_id not in requested or allocation.qty <= 0:
                raise OperationError("Источник или количество позиции не совпадают с заказом.")
            actual[allocation.order_item_id] += allocation.qty
    if dict(actual) != requested:
        raise OperationError("Распределите все товары по двум складам до начала сборки.")
    return True


def post_pick_enabled_for_order(order_id):
    """Opt in to safe physical-picking workflow for selected Tilda orders.

    Empty allowlist preserves preexisting flag semantics; a nonempty list
    permits only explicit positive CRM order IDs. This DOES NOT enable
    external MoySklad writes or the automatic warehouse worker.
    """
    import os
    if os.getenv("OMS_HATUNA_POST_PICK_ENABLED", "false").lower() != "true":
        return False
    selected = os.getenv("OMS_HATUNA_POST_PICK_ORDER_IDS", "").strip()
    if not selected:
        return True
    tokens = [token.strip() for token in selected.split(",")]
    if any(not token.isascii() or not token.isdecimal() or int(token) <= 0 for token in tokens):
        return False
    return type(order_id) is int and order_id in {int(token) for token in tokens}
