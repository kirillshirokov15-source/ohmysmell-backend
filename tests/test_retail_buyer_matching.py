"""Tilda retail buyer safety: no automatic contact guessing or provider writes."""
import asyncio
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.services.retail_buyer_binding import RetailBuyerService, verified_identity
from app.services.order_desk import DeskError
from app.services.manager_workspace import order_keyboard


@pytest.mark.parametrize("person,buyer,expected", [
    ({"email":"first@example.test", "phone":"", "name":"Кирилл"},
     {"id":"uuid", "email":"first@example.test", "name":"Другой"}, "email"),
    ({"email":"", "phone":"+7 925 111 22 33", "name":"Кирилл"},
     {"id":"uuid", "phone":"89251112233", "name":"Другой"}, "phone"),
    ({"email":"", "phone":"", "name":"Иван Петров"},
     {"id":"uuid", "name":"иван петров"}, "full_name"),
    ({"email":"", "phone":"", "name":"Кирилл"},
     {"id":"uuid", "name":"Кирилл"}, None),
    ({"email":"", "phone":"", "name":"Иван Петров"},
     {"id":"uuid", "name":"Иван Петров", "archived":True}, None),
    ({"email":"first@example.test", "phone":"", "name":"Кирилл"},
     {"id":"uuid", "email":"second@example.test", "name":"Кирилл"}, None),
])
def test_only_verified_nonarchived_matching(person, buyer, expected):
    assert verified_identity(buyer, **person) == expected


def test_missing_buyer_is_prominent_for_assembled_order(monkeypatch):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order", lambda _:True)
    order = Obj(id=9, revision=4, fulfillment_status="assembled", status="new",
                manual_fulfillment=True, needs_review=False, counterparty_id=None,
                payment_status="paid", delivery_method="pickup",
                delivery_status="pending", desk_draft_id=136)
    keyboard = order_keyboard(order, [], expanded=False)["inline_keyboard"]
    assert keyboard[0][0]["text"] == "Сопоставить покупателя МойСклад"
    assert keyboard[0][0]["callback_data"] == "order:buyer:9:4"


def test_already_mapped_buyer_hides_mapping_button(monkeypatch):
    monkeypatch.setattr("app.services.hatuna_post_pick.post_pick_enabled_for_order", lambda _:True)
    order = Obj(id=9, revision=5, fulfillment_status="assembled", status="new",
                manual_fulfillment=True, needs_review=False, counterparty_id="uuid",
                payment_status="paid", delivery_method="pickup",
                delivery_status="pending", desk_draft_id=136)
    keyboard = str(order_keyboard(order, [], expanded=True))
    assert "Сопоставить покупателя" not in keyboard


def test_unknown_identity_cannot_be_linked(monkeypatch):
    data = {"revision":4, "email":"", "phone":"", "name":"Кирилл",
            "counterparty_id":None}
    matcher = Obj(async_gateway=Obj(get_counterparty=AsyncMock(
        return_value={"id":"uuid", "name":"Кирилл"})))
    service = RetailBuyerService(matcher=matcher)
    service._read = AsyncMock(return_value=data)
    service._persist = AsyncMock()
    with pytest.raises(DeskError, match="не подтверждено"):
        asyncio.run(service.bind(9,111,-100123456,"uuid",4))
    service._persist.assert_not_awaited()


def test_verified_email_requires_explicit_binding(monkeypatch):
    data = {"revision":4, "email":"first@example.test",
            "phone":"", "name":"Кирилл", "counterparty_id":None}
    matcher = Obj(async_gateway=Obj(get_counterparty=AsyncMock(
        return_value={"id":"uuid","email":"first@example.test","name":"Кирилл"})))
    service = RetailBuyerService(matcher=matcher)
    service._read = AsyncMock(return_value=data)
    service._persist = AsyncMock(return_value="Связан")
    assert asyncio.run(service.bind(9,111,-100123456,"uuid",4))=="Связан"
    service._persist.assert_awaited_once()


def test_stale_order_cannot_change_buyer(monkeypatch):
    data = {"revision":5, "counterparty_id":None}
    matcher = Obj(async_gateway=Obj(get_counterparty=AsyncMock()))
    service = RetailBuyerService(matcher=matcher)
    service._read = AsyncMock(return_value=data)
    with pytest.raises(DeskError, match="Заказ изменился"):
        asyncio.run(service.bind(9,111,-100123456,"uuid",4))
    matcher.async_gateway.get_counterparty.assert_not_awaited()


def test_new_counterparty_requires_explicit_production_permission(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "environment","staging")
    service = RetailBuyerService(matcher=Obj())
    with pytest.raises(Exception, match="Counterparty creation is disabled"):
        asyncio.run(service.create_and_bind(9,111,-100123456,"Иван Петров",4))
