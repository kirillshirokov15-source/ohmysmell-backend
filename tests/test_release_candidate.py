import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from pydantic import ValidationError
from app.services.order_operations import OrderAction, OperationError, apply_action
from app.services.client_channel import parse_items


def order(**overrides):
    fields = dict(id=1, revision=0, status="new", fulfillment_status="new", payment_status="unpaid",
        needs_review=False, delivery_method="manual", delivery_status="pending", customer_name="Тест",
        customer_type="retail", phone="", telegram=None, source="telegram", counterparty_name=None,
        counterparty_id="cp", total=10001, items=[], paid_at=None, paid_by_manager_id=None,
        payment_note=None, delivery_reference=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def action(o, name, **kw):
    request = OrderAction(action=name, expected_revision=o.revision, idempotency_key=name, **kw)
    apply_action(o, request, 42, datetime.now(timezone.utc))


def test_fulfillment_and_payment_are_independent_and_money_immutable():
    o = order()
    action(o, "paid", note="Перевод подтверждён")
    for state in ("assembling", "assembled", "shipped"):
        action(o, state)
        assert o.fulfillment_status == state
    assert o.payment_status == "paid" and o.total == 10001 and o.status == "new"
    assert o.paid_by_manager_id == 42 and o.status_changed_by_manager_id == 42
    assert o.assembling_at <= o.assembled_at <= o.shipped_at
    assert o.paid_at and o.payment_note == "Перевод подтверждён"
    assert o.delivery_status == "dispatched"


@pytest.mark.parametrize("current,target", [("new", "assembled"), ("new", "shipped"), ("assembled", "assembling"), ("shipped", "assembled"), ("cancelled", "paid")])
def test_invalid_fulfillment(current, target):
    with pytest.raises(OperationError):
        action(order(fulfillment_status=current), target)


def test_review_blocks_assembly_until_resolved():
    o = order()
    action(o, "review")
    with pytest.raises(OperationError):
        action(o, "assembling")
    action(o, "resolve")
    action(o, "assembling")
    assert not o.needs_review


def test_stale_revision_cannot_change_payment():
    o = order(revision=2)
    with pytest.raises(OperationError):
        apply_action(o, OrderAction(action="paid", expected_revision=1, idempotency_key="p"), 1, datetime.now(timezone.utc))
    assert o.payment_status == "unpaid"


def test_duplicate_payment_does_not_overwrite_timestamp_or_actor():
    o = order()
    action(o, "paid")
    paid = o.paid_at
    with pytest.raises(OperationError):
        action(o, "paid")
    assert o.paid_at == paid


@pytest.mark.parametrize("state", ["shipped", "paid"])
def test_cancellation_requires_unpaid_unshipped(state):
    o = order(fulfillment_status="shipped") if state == "shipped" else order(payment_status="paid")
    with pytest.raises(OperationError):
        action(o, "cancel")


def test_cancel_preserves_technical_status():
    o = order()
    action(o, "cancel")
    assert o.fulfillment_status == "cancelled" and o.delivery_status == "cancelled" and o.status == "new"


def test_delivery_cannot_fake_shipment():
    with pytest.raises(OperationError):
        action(order(), "delivery", delivery_status="dispatched")


def test_delivery_lifecycle_and_reference():
    o = order()
    action(o, "delivery", delivery_method="cdek", delivery_reference="test-reference")
    action(o, "delivery", delivery_status="ready")
    for state in ("assembling", "assembled", "shipped"):
        action(o, state)
    action(o, "delivery", delivery_status="delivered")
    assert o.delivery_reference == "test-reference" and o.delivery_status == "delivered"


@pytest.mark.parametrize("value", ["Товар; 0", "Товар; -1", "Товар; 1.2", "Товар; true", "Товар; 10001", "Товар", "; 2", ""])
def test_client_quantity_validation(value):
    with pytest.raises(ValueError):
        parse_items(value)


def test_client_multiple_items():
    assert parse_items("Свеча; 2\nАромат; 3") == [{"name": "Свеча", "qty": 2}, {"name": "Аромат", "qty": 3}]


@pytest.mark.parametrize("value", ["8 (999) 123-45-67", "+7 999 123 45 67", "9991234567"])
def test_russian_phone_normalization(value):
    from app.services.customer_resolution_service import CustomerResolutionService
    from app.models.sales import CustomerIdentityType
    assert CustomerResolutionService.normalize(CustomerIdentityType.PHONE, value) == "+79991234567"


@pytest.mark.parametrize("value", ["@Test_User", "https://t.me/Test_User", "t.me/test_user/"])
def test_telegram_normalization(value):
    from app.services.customer_resolution_service import CustomerResolutionService
    from app.models.sales import CustomerIdentityType
    assert CustomerResolutionService.normalize(CustomerIdentityType.TELEGRAM, value) == "test_user"


def test_card_displays_two_warehouses_and_context_actions():
    from app.services.manager_workspace import order_card, order_keyboard
    item = SimpleNamespace(id=1, name="Свеча", qty=4, price=2500, item_total=10000)
    plans = [SimpleNamespace(warehouse_id=w, allocations=[SimpleNamespace(order_item_id=1, qty=2)]) for w in ("w1", "w2")]
    o = order(items=[item])
    card = order_card(o, plans)
    assert "Склад 1" in card and "Склад 2" in card and "Свеча: 2 шт." in card
    buttons = str(order_keyboard(o, plans))
    assert "Начать сборку" in buttons and "Заказ собран" not in buttons
    assert "Оплачен" in buttons
    assert "Оплачен" not in str(order_keyboard(order(payment_status="paid")))


def test_client_dispatcher_cannot_invoke_manager_actions():
    from app.bot.client_bot import create_dispatcher
    service = SimpleNamespace(handle=AsyncMock())
    dp = create_dispatcher(service)
    callback = SimpleNamespace(data="order:paid:1:0", answer=AsyncMock())
    asyncio.run(dp.callback_query.handlers[0].callback(callback))
    callback.answer.assert_awaited_once()
    service.handle.assert_not_awaited()


def test_client_rejects_someone_elses_contact():
    from app.bot.client_bot import create_dispatcher
    service = SimpleNamespace(handle=AsyncMock())
    dp = create_dispatcher(service)
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), from_user=SimpleNamespace(id=1),
        contact=SimpleNamespace(user_id=2), answer=AsyncMock())
    asyncio.run(dp.message.handlers[0].callback(message))
    service.handle.assert_not_awaited()


def test_client_ignores_group_messages():
    from app.bot.client_bot import create_dispatcher
    service = SimpleNamespace(handle=AsyncMock())
    dp = create_dispatcher(service)
    asyncio.run(dp.message.handlers[0].callback(SimpleNamespace(chat=SimpleNamespace(type="group"))))
    service.handle.assert_not_awaited()


def test_expired_order_callback_never_mutates():
    from app.bot import telegram_bot as bot
    from aiogram.exceptions import TelegramBadRequest
    callback = SimpleNamespace(answer=AsyncMock(side_effect=TelegramBadRequest(method=SimpleNamespace(), message="query is too old")),
        from_user=SimpleNamespace(id=1), data="order:paid:1:0")
    with patch.object(bot, "OrderOperations") as operations:
        asyncio.run(bot.order_callback(callback))
    operations.assert_not_called()


def test_unauthorized_order_callback_never_mutates():
    from app.bot import telegram_bot as bot
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1), data="order:paid:1:0")
    with patch.object(bot.manager_repository, "is_active_by_telegram_id", AsyncMock(return_value=False)), patch.object(bot, "OrderOperations") as operations:
        asyncio.run(bot.order_callback(callback))
    operations.assert_not_called()


@pytest.mark.parametrize("extra", [{"price": 1}, {"payment_status": "paid"}, {"manager_id": 1}])
def test_action_rejects_privileged_or_price_injection(extra):
    with pytest.raises(ValidationError):
        OrderAction(action="paid", expected_revision=0, idempotency_key="1", **extra)


def test_worker_rejects_shared_client_token(monkeypatch):
    from app.bot.runtime import validate_worker
    from app.config.settings import settings
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "same")
    monkeypatch.setenv("CLIENT_TELEGRAM_BOT_TOKEN", "same")
    with pytest.raises(ValueError, match="different"):
        validate_worker("client")


def test_worker_rejects_production(monkeypatch):
    from app.bot.runtime import validate_worker
    from app.config.settings import settings
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(ValueError):
        validate_worker("manager")


def test_api_does_not_import_or_start_polling():
    from pathlib import Path
    assert "app.bot" not in Path("app/main.py").read_text(encoding="utf-8")


def test_order_api_includes_operational_fields():
    from app.main import serialize_order
    o = order(customer_id=1, created_at=datetime.now(timezone.utc))
    response = serialize_order(o)
    assert response["fulfillment_status"] == "new" and response["payment_status"] == "unpaid"
    assert "paid_at" in response and response["revision"] == 0

@pytest.mark.parametrize("path,method", [("/orders/1/actions", "POST"), ("/orders/1/events", "GET")])
def test_operational_api_requires_internal_auth(path, method, monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "internal_api_token", "test-internal-token")
    from app.main import app
    from tests.asgi_client import request
    result = asyncio.run(request(app, method, path, payload={} if method == "POST" else None))
    assert result[0] == 401


def test_operational_api_requires_actor_and_forbids_price(monkeypatch):
    from app.main import app
    from app.config.settings import settings
    from tests.asgi_client import request
    monkeypatch.setattr(settings, "internal_api_token", "unit-api-token")
    headers = {"X-Internal-API-Token": "unit-api-token"}
    payload = {"action": "paid", "expected_revision": 0, "idempotency_key": "key"}
    assert asyncio.run(request(app, "POST", "/orders/1/actions", payload, headers))[0] == 422
    payload["price"] = 1
    headers["X-Manager-Telegram-ID"] = "1"
    assert asyncio.run(request(app, "POST", "/orders/1/actions", payload, headers))[0] == 422


def test_order_edit_failure_sends_fresh_card_without_repeating_action():
    from app.bot import telegram_bot as bot
    from aiogram.exceptions import TelegramBadRequest
    o = order(payment_status="paid", revision=1)
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1), data="order:paid:1:0",
        message=SimpleNamespace(edit_text=AsyncMock(side_effect=TelegramBadRequest(method=SimpleNamespace(), message="message to edit not found")), answer=AsyncMock()))
    service = SimpleNamespace(act=AsyncMock(return_value=o))
    with patch.object(bot.manager_repository, "is_active_by_telegram_id", AsyncMock(return_value=True)), patch.object(bot, "get_order", AsyncMock(return_value=order())), patch.object(bot, "OrderOperations", return_value=service), patch.object(bot, "shipments_for", AsyncMock(return_value=[])):
        asyncio.run(bot.order_callback(callback))
    service.act.assert_awaited_once()
    callback.message.answer.assert_awaited_once()


def test_temporary_order_db_failure_has_russian_message_without_secret():
    from app.bot import telegram_bot as bot
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1), data="order:refresh:1:0", message=SimpleNamespace(answer=AsyncMock()))
    with patch.object(bot.manager_repository, "is_active_by_telegram_id", AsyncMock(return_value=True)), patch.object(bot, "get_order", AsyncMock(side_effect=RuntimeError("private DB credential"))):
        asyncio.run(bot.order_callback(callback))
    assert "временно недоступен" in callback.message.answer.call_args.args[0]
    assert "credential" not in callback.message.answer.call_args.args[0]


def test_draft_large_ids_do_not_remove_matching_buttons():
    from app.services.draft_telegram_service import build_draft_keyboard
    draft = SimpleNamespace(id=2147483647, revision=100000, status="needs_review", counterparty_id=None,
        counterparty_candidates=[{"id": "a"*36, "name": "Counterparty"}],
        items=[SimpleNamespace(id=2147483647, product_id=None, match_status="ambiguous",
            candidates=[{"id": "b"*36, "name": "Product"}])])
    buttons = [b for row in build_draft_keyboard(draft)["inline_keyboard"] for b in row]
    assert any(":pick:" in b["callback_data"] for b in buttons)
    assert any(":cp:" in b["callback_data"] for b in buttons)
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_unversioned_draft_mutation_is_rejected():
    from app.bot import telegram_bot as bot
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1), data="draft:reject:1", message=SimpleNamespace(answer=AsyncMock()))
    with patch.object(bot.manager_repository, "is_active_by_telegram_id", AsyncMock(return_value=True)), patch.object(bot, "DraftOrderService") as service:
        asyncio.run(bot.draft_callback_handler(callback))
    service.assert_not_called()


def test_manager_email_projection_preserves_existing_identity():
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import Session
    from app.database.base import Base
    from app.models.customer import Customer, CustomerIdentity
    from app.models.order import Order
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        customer = Customer(customer_type="retail", display_name="Existing")
        customer.identities.append(CustomerIdentity(identity_type="email", normalized_value="existing@example.invalid", original_value="existing@example.invalid"))
        session.add(customer)
        session.flush()
        saved = Order(customer_id=customer.id, customer_name="Existing", phone="", customer_type="retail", source="manual", total=0)
        session.add(saved)
        session.commit()
        assert saved.customer_email == "existing@example.invalid"
        assert session.scalar(select(func.count()).select_from(CustomerIdentity)) == 1
    engine.dispose()


def test_manager_menu_exposes_all_daily_sections_in_russian():
    from app.bot.telegram_bot import main_menu
    labels = {button.text for row in main_menu.keyboard for button in row}
    assert {"📦 Новые заказы", "Требуют проверки", "Готовые заявки", "В сборке", "Собранные", "Отгруженные", "Неоплаченные", "Оплаченные", "Отменённые", "Поиск заказа"} <= labels


def test_order_card_contains_email():
    from app.services.manager_workspace import order_card
    assert "Email: existing@example.invalid" in order_card(order(customer_email="existing@example.invalid"))
