import asyncio
from unittest.mock import AsyncMock

import pytest

from app.models.sales import CustomerType, CustomerIdentityType
from app.services.customer_resolution_service import CustomerResolutionService
from app.services.draft_order_service import DraftOrderError
from app.services.draft_telegram_service import build_draft_card, build_draft_keyboard
from tests.test_email_pipeline import draft_service, email_message
from tests.test_sales_core import FakeCustomerRepository, existing_customer


@pytest.mark.parametrize("profile", list(CustomerType))
def test_email_type_price_and_buttons_do_not_depend_on_profile(profile):
    service, repository, _ = draft_service(profile, "cp")
    customer = existing_customer(profile, (CustomerIdentityType.EMAIL, "buyer@example.com"))
    customer.moysklad_counterparty_id = "cp"
    customers = FakeCustomerRepository([customer])
    service.customer_service = CustomerResolutionService(customers)
    draft = asyncio.run(service.ingest_email(email_message()))
    assert draft.customer_type == "wholesale" and draft.items[0].price == 15000
    assert customer.customer_type == profile  # never silently rewrite the profile
    policy = draft.contact_details["customer_type_policy"]
    assert policy["profile_conflict"] == (profile == CustomerType.RETAIL)
    card = build_draft_card(draft)
    assert "Тип клиента: Оптовый" in card and "Источник: Email" in card
    if profile == CustomerType.RETAIL:
        assert "профиль не изменён" in card
    buttons = [button for row in build_draft_keyboard(draft)["inline_keyboard"] for button in row]
    assert not any(button["callback_data"].startswith("draft:type:") for button in buttons)
    assert "тип клиента" not in (draft.review_notes or "")
    with pytest.raises(DraftOrderError, match="каналом"):
        asyncio.run(service.set_customer_type(draft.id, CustomerType.RETAIL))
    assert repository.customer_updated_to is None
    assert asyncio.run(service.ingest_email(email_message())).id == draft.id
    service.notifier.assert_awaited_once()


def test_unstructured_question_cannot_be_finalized_as_order():
    service, _, _ = draft_service(CustomerType.RETAIL, "cp")
    draft = asyncio.run(service.ingest_email(email_message(body="Здравствуйте! Когда вы работаете?")))
    assert draft.customer_type == "wholesale" and draft.status == "needs_review"
    assert draft.items == [] and draft.total is None
    assert "Вопрос или письмо без товарных строк" in build_draft_card(draft)
    assert "Новый заказ" not in build_draft_card(draft)
    with pytest.raises(DraftOrderError):
        asyncio.run(service.finalize(draft.id))


def test_website_channel_does_not_use_wholesale_price():
    service, _, customer = draft_service(CustomerType.WHOLESALE, "cp")
    draft = asyncio.run(service._ingest_resolved_email(email_message(), customer.resolution, source="website"))
    assert draft.customer_type == "retail"
    assert draft.items[0].price is None and draft.total is None
    assert draft.contact_details["customer_type_policy"]["profile_conflict"]
    assert not any("draft:type:" in button["callback_data"] for row in build_draft_keyboard(draft)["inline_keyboard"] for button in row)
