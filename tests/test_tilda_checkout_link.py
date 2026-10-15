"""Tilda checkout claim must never be derived from a predictable order ID."""
import asyncio
import hashlib
import secrets
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from app.integrations.tilda import normalize, TildaInvalid
from app.api import tilda
from app.services.tilda_checkout_link import checkout_nonce_hash, issue_checkout_link
from tests.asgi_client import request
from tests.test_tilda_orders import tilda_config, payload


def new_nonce():
    return secrets.token_urlsafe(32)


def test_checkout_nonce_normalized_as_digest_only(tilda_config):
    nonce = new_nonce()
    data = payload()
    data["oms_checkout_nonce_hash"] = checkout_nonce_hash(nonce)
    result = normalize(data)
    assert result["checkout_nonce_hash"] == hashlib.sha256(nonce.encode()).hexdigest()
    assert nonce not in str(result)
    assert result["reported_total"] == 3030
    assert "checkout_nonce_hash" not in normalize(payload())


@pytest.mark.parametrize("preference", ["any", "phone", "email", "telegram"])
def test_tilda_optional_contact_channel_keeps_order_totals_and_prices(tilda_config, preference):
    data = payload()
    data["oms_contact_preference"] = preference
    current = normalize(data)
    assert current["contact_preference"] == preference
    assert current["reported_total"] == normalize(payload())["reported_total"]
    assert current["items"] == normalize(payload())["items"]


def test_tilda_contact_channel_rejects_invalid_provider_values(tilda_config):
    data = payload()
    data["oms_contact_preference"] = "unknown"
    with pytest.raises(TildaInvalid, match="invalid_contact_preference"):
        normalize(data)


@pytest.mark.parametrize("nonce", ["short", "a" * 42, "a" * 44, "a" * 42 + "!", 123, True, {}, None])
def test_checkout_nonce_invalid_form_fails_closed(tilda_config, nonce):
    data = payload()
    data["oms_checkout_nonce_hash"] = nonce
    # An absent field and an explicitly null nonce are different.
    if nonce is None:
        assert "checkout_nonce_hash" not in normalize(data)
    else:
        with pytest.raises(TildaInvalid, match="invalid_checkout_nonce"):
            normalize(data)


def test_checkout_claim_endpoint_does_not_accept_order_id(tilda_config, monkeypatch):
    app = FastAPI()
    app.include_router(tilda.router)
    monkeypatch.setattr(tilda, "issue_checkout_link", AsyncMock(return_value={
        "deep_link": "https://t.me/synthetic_client_bot?start=" + "a" * 43,
        "expires_in_seconds": 1800}))
    nonce = new_nonce()
    status, data, _ = asyncio.run(request(app, "POST", "/integrations/tilda/telegram-link", {"checkout_nonce": nonce}))
    assert status == 200 and data["expires_in_seconds"] == 1800
    tilda.issue_checkout_link.assert_awaited_once_with(nonce)
    status, _, _ = asyncio.run(request(app, "POST", "/integrations/tilda/telegram-link", {"order_id": 10}))
    assert status == 422


@pytest.mark.staging
def test_checkout_claim_real_postgres_and_expired_link_retry(staging_sessions, tilda_config, monkeypatch):
    import importlib
    from sqlalchemy import select
    from app.models.draft_order import DraftOrder
    from app.models.order_desk import OrderDesk, OrderLink
    from app.services.order_desk import DeskService, TildaIntake, DeskError, now
    from datetime import timedelta

    factory, engine = staging_sessions
    for module in ("app.services.tilda_checkout_link", "app.services.order_desk", "app.database.session"):
        monkeypatch.setattr(importlib.import_module(module), "async_session", factory)
    monkeypatch.setenv("TILDA_CHECKOUT_LINK_ENABLED", "true")

    async def run():
        nonce = new_nonce()
        data = payload(uuid4().hex)
        data["oms_checkout_nonce_hash"] = checkout_nonce_hash(nonce)
        did = (await TildaIntake().submit(normalize(data)))["draft_id"]
        async with factory() as session:
            draft = await session.get(DraftOrder, did)
            assert draft.contact_details["tilda_checkout_nonce_hash"] == checkout_nonce_hash(nonce)
            assert nonce not in str(draft.contact_details)
        with pytest.raises(DeskError):
            await issue_checkout_link(new_nonce())
        first = await issue_checkout_link(nonce)
        token = first["deep_link"].split("start=")[1]
        async with factory() as session, session.begin():
            entry = await session.get(OrderLink, hashlib.sha256(token.encode()).hexdigest())
            entry.expires_at = now() - timedelta(seconds=1)
        second = await issue_checkout_link(nonce)
        assert second["deep_link"] != first["deep_link"]
        await DeskService().client(9000001, 1, "/start " + second["deep_link"].split("start=")[1])
        async with factory() as session:
            assert (await session.get(OrderDesk, did)).customer_telegram_id == 9000001
        with pytest.raises(DeskError):
            await issue_checkout_link(nonce)
        await engine.dispose()
    asyncio.run(run())
