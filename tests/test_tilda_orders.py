import asyncio
import json
import logging
from decimal import Decimal
from urllib.parse import urlencode
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from app.integrations.tilda import money, normalize, parse_body, product_map, TildaInvalid
from app.api import tilda
from app.api.safety import RequestSafetyMiddleware
from tests.asgi_client import request


def payload(identifier="synthetic-order"):
    return {"tranid": identifier, "Name": "SYNTHETIC Buyer", "Email": "synthetic@example.invalid",
        "Phone": "+70000000000", "Comments": "Synthetic only", "currency": "RUB", "amount": "30.30",
        "products": [{"externalid": "sku-a", "name": "SYNTHETIC A", "quantity": "2", "price": "10.10"},
                     {"externalid": "sku-b", "name": "SYNTHETIC B", "quantity": "1", "price": "10.10"}]}


@pytest.fixture
def tilda_config(monkeypatch):
    monkeypatch.setattr("app.integrations.moysklad.client.MOYSKLAD_TOKEN", "synthetic-no-network")
    monkeypatch.setenv("ORDER_DESK_SEND_ENABLED", "true")
    monkeypatch.setenv("TILDA_ENABLED", "true")
    monkeypatch.setenv("TILDA_WEBHOOK_SECRET", "synthetic-tilda-secret-32-characters-only")
    monkeypatch.setenv("TILDA_ALLOW_QUERY_SECRET", "false")
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", "{}")
    monkeypatch.setenv("TILDA_ITEM_FIELD_MAP_JSON", "{}")
    monkeypatch.setenv("TILDA_PRODUCT_MAP_JSON", json.dumps({key: {"product_id": key, "retail_price_minor": 1010}
        for key in ("sku-a", "sku-b")}))
    monkeypatch.setenv("MANAGER_TELEGRAM_CHAT_ID", "-100123456")
    monkeypatch.setenv("MANAGER_TELEGRAM_USER_IDS", "111,222")
    monkeypatch.setenv("CLIENT_TELEGRAM_BOT_USERNAME", "synthetic_client_bot")
    monkeypatch.setenv("CLIENT_TELEGRAM_LINK_TTL_MINUTES", "30")
    monkeypatch.setattr("app.services.product_matching_service.ProductMatchingService.products_async",
        AsyncMock(return_value=[{"id": key, "article": key, "name": key}
                                for key in ("sku-a", "sku-b")]))


@pytest.mark.parametrize("value,expected", [("1.01",101),("1,99",199),(Decimal("10.10"),1010),(0,0),("0.29",29)])
def test_exact_money(value, expected):
    assert money(value) == expected


@pytest.mark.parametrize("value", [True, 1.2, "NaN", "Infinity", "-1", "1.001", None, "1000000001"])
def test_invalid_money(value):
    with pytest.raises(TildaInvalid): money(value)


def test_json_form_arrays_and_optional_fields(tilda_config):
    data = payload()
    normalized = normalize(parse_body(json.dumps(data).encode(), "application/json"))
    assert len(normalized["items"]) == 2 and normalized["reported_total"] == 3030
    data["products"] = json.dumps(data["products"])
    assert normalize(parse_body(urlencode(data).encode(), "application/x-www-form-urlencoded")) == normalized
    minimal = {"tranid":"minimal", "amount":"1.00", "products[0][externalid]":"sku-a",
        "products[0][name]":"A", "products[0][quantity]":"1", "products[0][price]":"1.00", "unrelated":"ignored"}
    parsed = normalize(parse_body(urlencode(minimal).encode(), "application/x-www-form-urlencoded"))
    assert parsed["email"] == parsed["phone"] == parsed["comment"] == ""
    assert parsed["problems"] == []


@pytest.mark.parametrize("qty", ["0", "-1", "1.2", "NaN", True, None, "10001"])
def test_invalid_quantity(qty):
    data = payload(); data["products"][0]["quantity"] = qty
    with pytest.raises(TildaInvalid): normalize(data)


@pytest.mark.parametrize("cart", [[], None, "oops", {}, [1]])
def test_invalid_cart(cart):
    data = payload(); data["products"] = cart
    with pytest.raises(TildaInvalid): normalize(data)


def test_missing_id_never_hashes_identical_baskets():
    data = payload(); del data["tranid"]
    with pytest.raises(TildaInvalid): normalize(data)
    assert normalize(data, "delivery-a")["external_id"] != normalize(data, "delivery-b")["external_id"]


def test_total_review_and_configurable_contract(monkeypatch):
    data = payload(); data["amount"] = "30.31"
    assert "total_mismatch" in normalize(data)["problems"]
    data["amount"] = "bad"
    assert "invalid_total" in normalize(data)["problems"]
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", '{"items":"payment.products","total":"payment.amount"}')
    data = payload(); data["payment"] = {"products":data.pop("products"), "amount":data.pop("amount")}
    assert normalize(data)["problems"] == []


def test_no_wholesale_fallback(monkeypatch):
    monkeypatch.setenv("TILDA_PRODUCT_MAP_JSON", '{"sku-a":{"product_id":"a","wholesale_price_minor":100}}')
    assert product_map()["sku-a"]["retail_price_minor"] is None


@pytest.mark.parametrize("secret,enabled,status", [("wrong","true",401),("","true",401),("correct","false",503),("missing-config","true",503)])
def test_webhook_auth_fails_closed(tilda_config, monkeypatch, secret, enabled, status):
    monkeypatch.setenv("TILDA_ENABLED", enabled)
    if secret == "missing-config": monkeypatch.delenv("TILDA_WEBHOOK_SECRET")
    if secret == "correct": secret = "synthetic-tilda-secret-32-characters-only"
    app = FastAPI(); app.include_router(tilda.router)
    actual = asyncio.run(request(app, "POST", "/integrations/tilda/orders", payload(), headers={"X-Tilda-Secret":secret}))[0]
    assert actual == status


def test_webhook_form_query_redaction_and_no_pii_logs(tilda_config, monkeypatch, caplog):
    monkeypatch.setenv("TILDA_ALLOW_QUERY_SECRET", "true")
    submit = AsyncMock(return_value={"draft_id":1,"duplicate":False})
    monkeypatch.setattr(tilda.TildaIntake, "submit", submit)
    app = FastAPI(); app.include_router(tilda.router); app.add_middleware(RequestSafetyMiddleware)
    monkeypatch.setattr(tilda.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(tilda.logger, "propagate", False)
    data=payload(); data["products"]=json.dumps(data["products"])
    with caplog.at_level(logging.INFO):
        response=asyncio.run(request(app,"POST","/integrations/tilda/orders?secret=synthetic-tilda-secret-32-characters-only",
            headers={"Content-Type":"application/x-www-form-urlencoded"}, content=urlencode(data).encode()))
    assert response[0]==200 and submit.await_args.args[0]["items"][0]["qty"]==2
    assert "tilda_webhook_accepted" in caplog.text
    for sensitive in (data["Email"],data["Phone"],data["Name"],"synthetic-tilda-secret-32-characters-only"):
        assert sensitive not in caplog.text


def test_duplicate_fields_rejected():
    with pytest.raises(TildaInvalid): parse_body(b"tranid=a&tranid=b", "application/x-www-form-urlencoded")


def test_client_poll_does_not_acknowledge_uncommitted_update(monkeypatch):
    from app.bot.runtime import durable_client_poll
    from types import SimpleNamespace
    class Finished(BaseException): pass
    async def run():
        offsets=[]; handled=[]
        update=SimpleNamespace(update_id=27)
        async def bot(method):
            offsets.append(method.offset)
            if len(offsets)>1:raise Finished()
            return [update]
        async def handle(bot, update):
            handled.append(update.update_id)
            if len(handled)==1:raise RuntimeError("synthetic DB unavailable")
        async def sleep(_):pass
        monkeypatch.setattr("app.bot.runtime.asyncio.sleep",sleep)
        dp=SimpleNamespace(feed_update=handle,resolve_used_update_types=lambda:["message"])
        with pytest.raises(Finished): await durable_client_poll(dp,bot)
        assert handled==[27,27] and offsets==[None,28]
    asyncio.run(run())

def test_real_cart_nested_order_identifier_and_items(monkeypatch):
    from app.integrations.tilda import normalize
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", "{}")
    monkeypatch.setenv("TILDA_ITEM_FIELD_MAP_JSON", "{}")
    sample = {"payment": {"orderid": "order-42", "amount": "560", "products": [
        {"externalid": "MARV001", "name": "Marvis", "quantity": "1", "price": "560"}]},
        "Name": "SYNTHETIC", "Email": "test@example.invalid", "Phone": "+70000000000"}
    parsed = normalize(sample)
    assert parsed["external_id"] == "source:payment.orderid:order-42"
    assert parsed["items"][0]["externalid"] == "MARV001"
    assert parsed["reported_total"] == 56000
    assert parsed["problems"] == []


def test_multiple_distinct_cart_ids_do_not_collide(monkeypatch):
    from app.integrations.tilda import normalize
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", "{}")
    sample = payload()
    sample.pop("tranid")
    sample["orderid"] = "1"
    one = normalize(sample)
    sample["orderid"] = "2"
    two = normalize(sample)
    assert one["external_id"] != two["external_id"]


def test_missing_real_tilda_id_still_rejected(monkeypatch):
    from app.integrations.tilda import normalize, TildaInvalid
    monkeypatch.setenv("TILDA_FIELD_MAP_JSON", "{}")
    sample = payload()
    sample.pop("tranid")
    with pytest.raises(TildaInvalid, match="stable_external_id_required"):
        normalize(sample)


def test_manager_card_review_blockers_are_actionable():
    from types import SimpleNamespace
    from app.services.desk_display import review_blockers, REVIEW_PROBLEM_LABELS
    item = SimpleNamespace(product_id="prod-1", match_status="matched", price=56000, qty=1)
    draft = SimpleNamespace(total=None, items=[item],
        contact_details={"tilda_review_problems": ["invalid_total"]})
    problems = review_blockers(draft)
    assert problems == ["invalid_total"]
    assert "Tilda" in REVIEW_PROBLEM_LABELS[problems[0]]
    draft.total = 56000
    draft.contact_details["tilda_review_problems"] = []
    assert review_blockers(draft) == []
    item.product_id = None
    assert review_blockers(draft) == ["sku_unmatched"]


def test_new_and_claimed_site_drafts_are_pending_confirmation():
    from types import SimpleNamespace
    from app.services.desk_status import stage_label
    draft = SimpleNamespace(status="needs_review")
    desk = SimpleNamespace(order_id=None, stage="new")
    assert stage_label(desk, draft) == "Ожидает подтверждения"
    desk.stage = "working"
    assert stage_label(desk, draft) == "Ожидает подтверждения"
    desk.stage = "awaiting_confirmation"
    assert stage_label(desk, draft) == "Ожидает подтверждения"
