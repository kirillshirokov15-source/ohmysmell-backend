import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.config.settings import Settings, settings
from app.integrations.moysklad.client import MoySkladClient
from app.services.draft_order_service import DraftOrderService
from app.services.draft_telegram_service import (
    build_draft_card,
    build_draft_keyboard,
)
from app.services.counterparty_matching_service import CounterpartyMatchingService
from app.services.telegram_display import (
    customer_type_label,
    draft_status_label,
    product_match_label,
    telegram_rubles,
)
from app.services.moysklad_order_service import (
    MoySkladOrderError,
    MoySkladOrderService,
)
from app.services.order_lifecycle import OrderStatus
from app.workers.staging_fake_email import (
    BODY,
    SUBJECT,
    configure_staging_environment,
)


def test_external_writes_default_to_false():
    assert settings.external_writes_enabled is False


def test_wholesale_price_type_default_is_exact_utf8(monkeypatch):
    monkeypatch.delenv("MOYSKLAD_WHOLESALE_PRICE_TYPE", raising=False)
    assert Settings().moysklad_wholesale_price_type == "Цена продажи"
    assert Settings().moysklad_retail_price_type == ""


def test_moysklad_write_is_blocked_before_local_or_external_work():
    client = Mock()
    with (
        patch.object(settings, "external_writes_enabled", False),
        patch(
            "app.services.moysklad_order_service.get_order",
            new=AsyncMock(),
        ) as get_order,
    ):
        with pytest.raises(
            MoySkladOrderError,
            match="^External writes are disabled$",
        ):
            asyncio.run(
                MoySkladOrderService(client).create_from_crm_order(1)
            )

    get_order.assert_not_awaited()
    client.create_customer_order.assert_not_called()


def test_read_only_moysklad_operation_is_unaffected():
    client = MoySkladClient.__new__(MoySkladClient)
    client.base_url = "https://example.invalid/api"
    client.headers = {}
    client.session = Mock()
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"rows": [{"id": "product-1"}]}

    with (
        patch.object(settings, "external_writes_enabled", False),
        patch.object(client.session, "get", return_value=response) as get,
    ):
        assert client.get_products() == [{"id": "product-1"}]

    get.assert_called_once()


def test_moysklad_products_are_loaded_from_later_pages():
    client = MoySkladClient.__new__(MoySkladClient)
    client.base_url = "https://example.invalid/api"
    client.headers = {}
    client.session = Mock()
    first = Mock()
    first.raise_for_status.return_value = None
    first.json.return_value = {
        "meta": {"size": 1001},
        "rows": [{"id": f"p-{index}"} for index in range(1000)],
    }
    second = Mock()
    second.raise_for_status.return_value = None
    second.json.return_value = {
        "meta": {"size": 1001},
        "rows": [{"id": "last-page-product"}],
    }
    client.session.get.side_effect = [first, second]

    products = client.get_products()

    assert len(products) == 1001
    assert products[-1]["id"] == "last-page-product"
    assert [call.kwargs["params"]["offset"] for call in client.session.get.call_args_list] == [
        0,
        1000,
    ]


def test_telegram_product_action_matches_unresolved_state():
    def draft_with(*statuses):
        return SimpleNamespace(
            id=1,
            status="needs_review",
            items=[
                SimpleNamespace(
                    id=index,
                    match_status=status,
                    product_id=None,
                    candidates=[],
                )
                for index, status in enumerate(statuses, start=1)
            ],
            counterparty_candidates=[],
            counterparty_id=None,
        )

    ambiguous = build_draft_keyboard(draft_with("ambiguous"))
    not_found = build_draft_keyboard(draft_with("not_found"))
    mixed = build_draft_keyboard(draft_with("ambiguous", "not_found"))

    def labels(keyboard):
        return [button["text"] for row in keyboard["inline_keyboard"] for button in row]

    assert "Выбрать товар" in labels(ambiguous)
    assert "Сопоставить товары" in labels(not_found)
    assert "Сопоставить товары" in labels(mixed)
    assert "Выбрать контрагента" in labels(not_found)
    assert "Подтвердить заказ" not in labels(not_found)


def test_candidate_buttons_mark_only_selected_product():
    candidates = [
        {"id": "p1", "name": "First long product name", "article": "A1", "score": 0.9},
        {"id": "p2", "name": "Second long product name", "article": "A2", "score": 0.8},
    ]

    def labels(selected):
        keyboard = build_draft_keyboard(SimpleNamespace(
            id=1,
            status="needs_review",
            counterparty_id="cp1",
            counterparty_candidates=[],
            items=[SimpleNamespace(
                id=2,
                match_status="ambiguous",
                product_id=selected,
                candidates=candidates,
            )],
        ))
        return [
            button["text"]
            for row in keyboard["inline_keyboard"]
            for button in row
            if "A1" in button["text"] or "A2" in button["text"]
        ]

    assert all(not label.startswith("✓") for label in labels(None))
    selected = labels("p1")
    assert selected[0].startswith("✓ A1")
    assert not selected[1].startswith("✓")


def test_russian_labels_keep_callback_data_unchanged():
    draft = SimpleNamespace(
        id=22,
        status="needs_review",
        counterparty_id=None,
        counterparty_candidates=[],
        items=[SimpleNamespace(
            id=7,
            match_status="ambiguous",
            product_id=None,
            candidates=[{
                "id": "product-uuid",
                "name": "Product name",
                "article": "ART-1",
            }],
        )],
    )
    buttons = [
        button
        for row in build_draft_keyboard(draft)["inline_keyboard"]
        for button in row
    ]
    by_text = {button["text"]: button["callback_data"] for button in buttons}
    assert by_text["Подтвердить: опт"] == "draft:type:wholesale:22"
    assert by_text["Подтвердить: розница"] == "draft:type:retail:22"
    assert by_text["Выбрать контрагента"] == "draft:counterparty_select:22"
    assert by_text["Выбрать товар"] == "draft:ambiguous:22"
    assert by_text["Отклонить"] == "draft:reject:22"
    candidate = next(button for button in buttons if "ART-1" in button["text"])
    assert candidate["callback_data"] == "draft:product:22:7:product-uuid"


def test_draft_card_is_fully_localized_and_formats_minor_units():
    draft = SimpleNamespace(
        id=2,
        status="needs_review",
        customer_name="OhMySmell Staging Test",
        sender_email="staging-test@ohmysmell.local",
        customer_type="unknown",
        subject="STAGING TEST ORDER",
        counterparty_name=None,
        counterparty_id=None,
        counterparty_candidates=[],
        total=None,
        items=[
            SimpleNamespace(
                raw_product_text="Chanel Allure Homme Sport",
                qty=2,
                match_status="ambiguous",
                price=None,
                item_total=None,
            ),
            SimpleNamespace(
                raw_product_text="Marvis Classic Strong Mint 85 ml",
                qty=3,
                match_status="matched",
                price=56000,
                item_total=168000,
            ),
        ],
    )
    card = build_draft_card(draft)
    assert "📨 Новый заказ из почты" in card
    assert "Черновик №2" in card
    assert "Тип клиента: Не определён" in card
    assert "Статус: требует проверки" in card
    assert "3 шт. × 560 ₽ = 1 680 ₽" in card
    assert "Необходимо определить тип клиента" in card
    assert "Необходимо выбрать контрагента" in card
    assert "Необходимо выбрать товар: Chanel Allure Homme Sport" in card
    for internal in (
        "needs_review",
        "customer_type=unknown",
        "matched",
        "ambiguous",
        "not_found",
    ):
        assert internal not in card


def test_display_mappings_and_telegram_money_are_russian():
    assert customer_type_label("unknown") == "Не определён"
    assert customer_type_label("wholesale") == "Оптовый"
    assert customer_type_label("retail") == "Розничный"
    assert draft_status_label("needs_review") == "требует проверки"
    assert product_match_label("matched") == "найден"
    assert product_match_label("ambiguous") == "нужно выбрать"
    assert product_match_label("not_found") == "не найден"
    assert telegram_rubles(56000) == "560 ₽"
    assert telegram_rubles(580000) == "5 800 ₽"
    assert telegram_rubles(1328000) == "13 280 ₽"


def test_fully_priced_draft_card_shows_exact_item_and_order_totals():
    draft = SimpleNamespace(
        id=2,
        status="needs_review",
        customer_name="Test customer",
        sender_email="test@example.invalid",
        customer_type="wholesale",
        subject="Test",
        counterparty_name=None,
        counterparty_id=None,
        counterparty_candidates=[],
        total=1328000,
        items=[
            SimpleNamespace(
                raw_product_text="Chanel Allure Homme Sport",
                qty=2,
                match_status="matched",
                price=580000,
                item_total=1160000,
            ),
            SimpleNamespace(
                raw_product_text="Marvis Classic Strong Mint 85 ml",
                qty=3,
                match_status="matched",
                price=56000,
                item_total=168000,
            ),
        ],
    )

    card = build_draft_card(draft)
    assert "2 шт. × 5 800 ₽ = 11 600 ₽" in card
    assert "3 шт. × 560 ₽ = 1 680 ₽" in card
    assert "Итого: 13 280 ₽" in card


def test_zero_relevant_counterparties_fall_back_to_recent_existing():
    provider = SimpleNamespace(
        search_counterparties=lambda query: [],
        get_recent_counterparties=lambda limit=10: [{
            "id": "cp-existing",
            "name": "Existing Counterparty",
            "email": "existing@example.com",
        }],
    )
    candidates = CounterpartyMatchingService(provider).fallback_candidates(
        "unknown@example.com"
    )
    assert candidates == [{
        "id": "cp-existing",
        "name": "Existing Counterparty",
        "email": "existing@example.com",
        "phone": None,
    }]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"customer_type": "unknown"}, "customer type is unknown"),
        ({"item_status": "ambiguous", "product_id": None}, "unresolved product"),
        ({"counterparty_id": None}, "counterparty is not selected"),
        ({"price": None, "item_total": None}, "price is missing"),
    ],
)
def test_finalize_guards_block_incomplete_draft(overrides, message):
    values = {
        "customer_type": "wholesale",
        "counterparty_id": "cp1",
        "item_status": "matched",
        "product_id": "p1",
        "price": 100,
        "item_total": 100,
        "total": 100,
    }
    values.update(overrides)
    reviewed = SimpleNamespace(
        id=5,
        status=OrderStatus.READY,
        customer_type=values["customer_type"],
        counterparty_id=values["counterparty_id"],
        total=values["total"],
        items=[SimpleNamespace(
            raw_product_text="Product",
            match_status=values["item_status"],
            product_id=values["product_id"],
            price=values["price"],
            item_total=values["item_total"],
        )],
    )
    repository = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(
            id=5, finalized_order_id=None
        )),
        finalize=AsyncMock(),
    )
    service = DraftOrderService.__new__(DraftOrderService)
    service.repository = repository
    service.review = AsyncMock(return_value=reviewed)

    with pytest.raises(Exception, match=message):
        asyncio.run(service.finalize(5))

    repository.finalize.assert_not_awaited()


def test_finalize_blocks_missing_draft_total():
    reviewed = SimpleNamespace(
        id=5,
        status=OrderStatus.READY,
        customer_type="wholesale",
        counterparty_id="cp1",
        total=None,
        items=[SimpleNamespace(
            raw_product_text="Product",
            match_status="matched",
            product_id="p1",
            price=100,
            item_total=100,
        )],
    )
    repository = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(id=5, finalized_order_id=None)),
        finalize=AsyncMock(),
    )
    service = DraftOrderService.__new__(DraftOrderService)
    service.repository = repository
    service.review = AsyncMock(return_value=reviewed)

    with pytest.raises(Exception, match="draft total is missing"):
        asyncio.run(service.finalize(5))

    repository.finalize.assert_not_awaited()


def test_enabling_external_writes_reaches_mocked_integration():
    client = Mock()
    client.create_customer_order.return_value = {
        "id": "ms-order-1",
        "name": "MS-1",
    }
    order = SimpleNamespace(
        id=1,
        moysklad_order_id=None,
        moysklad_order_name=None,
        counterparty_id="counterparty-1",
        customer_name="Buyer",
        phone="",
        telegram=None,
        comment=None,
        items=[
            SimpleNamespace(
                product_id="product-1",
                name="Product",
                price=9999,
                qty=2,
            )
        ],
    )

    with (
        patch.object(settings, "external_writes_enabled", True),
        patch(
            "app.services.moysklad_order_service.get_order",
            new=AsyncMock(return_value=order),
        ),
        patch(
            "app.services.moysklad_order_service.set_moysklad_order",
            new=AsyncMock(),
        ) as set_moysklad_order,
    ):
        result = asyncio.run(
            MoySkladOrderService(client).create_from_crm_order(1)
        )

    assert result["already_exists"] is False
    client.create_customer_order.assert_called_once()
    set_moysklad_order.assert_awaited_once_with(
        order_id=1,
        moysklad_order_id="ms-order-1",
        moysklad_order_name="MS-1",
    )


def test_finalize_draft_remains_local_only():
    draft = SimpleNamespace(id=5, finalized_order_id=None)
    ready = SimpleNamespace(
        id=5,
        status=OrderStatus.READY,
        customer_type="wholesale",
        counterparty_id="cp1",
        total=1328000,
        items=[SimpleNamespace(
            raw_product_text="Product",
            match_status="matched",
            product_id="p1",
            price=100,
            item_total=100,
        )],
    )
    local_order = SimpleNamespace(
        id=10,
        status=OrderStatus.NEW,
        total=1328000,
    )
    repository = SimpleNamespace(
        get=AsyncMock(return_value=draft),
        finalize=AsyncMock(return_value=local_order),
    )
    service = DraftOrderService(repository=repository)
    service.review = AsyncMock(return_value=ready)

    with patch.object(settings, "external_writes_enabled", False):
        result = asyncio.run(service.finalize(5))

    assert result is local_order
    assert result.total == 1328000
    assert isinstance(result.total, int)
    repository.finalize.assert_awaited_once_with(5)


def test_staging_fake_message_uses_expected_parser_input():
    from app.services.email_parser import EmailParser

    lines = EmailParser().parse_lines(BODY)
    assert SUBJECT == "STAGING TEST ORDER"
    assert [(line.raw_product_text, line.qty) for line in lines] == [
        ("Chanel Allure Homme Sport", 2),
        ("Marvis Classic Strong Mint 85 ml", 3),
    ]


def test_staging_fake_entrypoint_requires_external_writes_disabled(
    monkeypatch,
):
    monkeypatch.setenv("EXTERNAL_WRITES_ENABLED", "true")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://prod@production.invalid/prod"
    )
    monkeypatch.setenv(
        "STAGING_DATABASE_URL", "postgresql://stage@staging.invalid/stage"
    )

    with pytest.raises(
        RuntimeError,
        match="EXTERNAL_WRITES_ENABLED must be false",
    ):
        configure_staging_environment()


def test_staging_fake_entrypoint_selects_only_distinct_staging_database(
    monkeypatch,
):
    staging_url = "postgresql://stage@staging.invalid/stage"
    monkeypatch.setenv("EXTERNAL_WRITES_ENABLED", "false")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://prod@production.invalid/prod"
    )
    monkeypatch.setenv("STAGING_DATABASE_URL", staging_url)

    configure_staging_environment()

    assert os.environ["DATABASE_URL"] == staging_url
