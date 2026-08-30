from enum import StrEnum


class OrderStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"
    NEW = "new"
    CREATED_IN_MOYSKLAD = "created_in_moysklad"
    REJECTED = "rejected"


ALLOWED_ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset(
        {OrderStatus.NEEDS_REVIEW, OrderStatus.READY, OrderStatus.REJECTED}
    ),
    OrderStatus.NEEDS_REVIEW: frozenset(
        {OrderStatus.READY, OrderStatus.REJECTED}
    ),
    OrderStatus.READY: frozenset(
        {OrderStatus.NEEDS_REVIEW, OrderStatus.NEW, OrderStatus.REJECTED}
    ),
    OrderStatus.NEW: frozenset(
        {OrderStatus.CREATED_IN_MOYSKLAD, OrderStatus.REJECTED}
    ),
    OrderStatus.CREATED_IN_MOYSKLAD: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class InvalidOrderTransitionError(Exception):
    pass


def ensure_order_transition(current: str, target: str) -> None:
    current_status = OrderStatus(current)
    target_status = OrderStatus(target)
    if target_status not in ALLOWED_ORDER_TRANSITIONS[current_status]:
        raise InvalidOrderTransitionError(
            f"Недопустимый переход заказа: {current_status.value} -> "
            f"{target_status.value}"
        )
