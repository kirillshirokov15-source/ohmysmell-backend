"""Isolated, read-only smoke check for fast manager cards.

Run only with fake credentials on a disposable Railway staging service.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import time


def main():
    count = 0
    for path in Path("app").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += 1

    from app.bot.manager_fast_cards import enabled
    from app.bot.telegram_bot import dp
    from app.services.manager_workspace import is_closed_order, order_keyboard

    assert enabled(), "fast card feature flag was not enabled in smoke environment"
    assert dp.callback_query.handlers, "manager bot callback routes are missing"

    order = SimpleNamespace(
        id=15, revision=1, source="email", fulfillment_status="shipped",
        payment_status="paid", delivery_status="pending", customer_name="Test",
        total=540000,
    )
    assert is_closed_order(order)
    assert "workspace:home" in repr(order_keyboard(order))
    print(f"MANAGER_FAST_CARDS_SMOKE_OK parsed={count} handlers={len(dp.callback_query.handlers)}", flush=True)
    time.sleep(120)


if __name__ == "__main__":
    main()
