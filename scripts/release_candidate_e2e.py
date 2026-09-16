"""Explicit staging-only RC acceptance. Never contacts production or writes providers.
Run with project Python, STAGING_DATABASE_URL and verified remote staging API token.
Client/manager input is simulated; manager notification delivery is tested separately.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4


def main():
    from dotenv import load_dotenv
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config, activate_staging_config
    activate_staging_config(validate_staging_config(dict(os.environ)))
    from app.config.settings import settings
    settings.environment = "staging"
    assert not settings.external_writes_enabled
    from app.integrations.http_tls import verified_session
    token = os.getenv("STAGING_INTERNAL_API_TOKEN") or Path(".staging-artifacts/remote_internal_token").read_text().strip()
    base = "https://ohmysmell-backend-staging-staging.up.railway.app"
    uid = "rc-" + uuid4().hex
    report = {"run": uid, "environment": "staging", "external_writes": False, "metrics_ms": {}, "checks": []}
    http = verified_session()
    headers = {"X-Internal-API-Token": token}
    def call(method, path, expected=200, payload=None, extra=None, label=None):
        started = perf_counter()
        response = http.request(method, base+path, json=payload, headers={**headers, **(extra or {})}, timeout=(10, 90))
        report["metrics_ms"][label or f"{method} {path}"] = round((perf_counter()-started)*1000, 2)
        assert response.status_code == expected, f"HTTP {response.status_code}: {method} {path}"
        assert token not in response.text and "Traceback" not in response.text
        return response.json()
    async def scenario():
        from sqlalchemy import select, func
        from app.database.session import async_session, engine
        from app.models.manager import Manager
        from app.models.order import Order
        from app.models.draft_order import DraftOrder
        from app.models.operations import OrderEvent
        from app.models.fulfillment import Shipment, ExternalOperation
        from app.services.client_channel import ClientChannel
        from app.repositories.draft_order_repository import DraftOrderRepository
        from app.repositories.order_repository import get_order
        from app.services.manager_workspace import shipments_for, order_card
        async with async_session() as session:
            manager = (await session.execute(select(Manager).where(Manager.is_active.is_(True)).order_by(Manager.id).limit(1))).scalar_one()
        envelope = {"external_message_id": uid, "sender_email": uid+"@example.invalid", "sender_name": "RC staging synthetic",
            "subject": "RC acceptance — local operations only", "body_text": "MARV007 x1", "received_at": datetime.now(timezone.utc).isoformat()}
        draft = await asyncio.to_thread(call, "POST", "/internal/email/messages", payload=envelope, label="product_matching")
        assert len(draft["items"]) == 1 and draft["items"][0]["match_status"] == "matched"
        draft_id = draft["id"]
        async def db_draft(status):
            async with async_session() as session:
                saved = await session.get(DraftOrder, draft_id)
                assert saved.status == status
        await db_draft("needs_review")
        duplicate = await asyncio.to_thread(call, "POST", "/internal/email/messages", payload=envelope)
        assert duplicate["id"] == draft_id
        draft = await asyncio.to_thread(call, "POST", f"/draft-orders/{draft_id}/customer-type", payload={"customer_type": "wholesale"})
        # Deliberately fake local counterparty: no remote customer creation/link mutation.
        draft = await asyncio.to_thread(call, "POST", f"/draft-orders/{draft_id}/counterparty", payload={"counterparty_id": uid, "counterparty_name": "RC fake local counterparty"})
        await db_draft("ready")
        finalized = await asyncio.to_thread(call, "POST", f"/draft-orders/{draft_id}/finalize")
        order_id = finalized["order"]["id"]
        replay = await asyncio.to_thread(call, "POST", f"/draft-orders/{draft_id}/finalize")
        assert replay["order"]["id"] == order_id
        await db_draft("new")
        async with async_session() as session:
            original = await session.get(Order, order_id)
            assert original.fulfillment_status == "new" and original.payment_status == "unpaid"
            total = original.total
        await asyncio.to_thread(call, "POST", f"/orders/{order_id}/allocations")
        manager_headers = {"X-Manager-Telegram-ID": str(manager.telegram_id)}
        for revision, (action, extra) in enumerate([("delivery", {"delivery_method": "manual"}), ("assembling", {}), ("assembled", {}), ("shipped", {}), ("paid", {"note": "Synthetic RC payment flag; no real money"})]):
            payload = {"action": action, "expected_revision": revision, "idempotency_key": uid+action, **extra}
            response = await asyncio.to_thread(call, "POST", f"/orders/{order_id}/actions", payload=payload, extra=manager_headers, label=action+"_api")
            assert response["revision"] == revision+1
            await asyncio.to_thread(call, "POST", f"/orders/{order_id}/actions", payload=payload, extra=manager_headers, label=action+"_replay")
            async with async_session() as session:
                saved = await session.get(Order, order_id)
                assert saved.revision == revision+1 and saved.total == total and saved.moysklad_order_id is None
                assert await session.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == order_id)) == revision+1
                if action == "paid":
                    assert saved.paid_at and saved.paid_by_manager_id == manager.id and saved.shipped_at and saved.assembled_at
                elif action != "delivery":
                    assert saved.fulfillment_status == action
        await asyncio.to_thread(call, "POST", f"/orders/{order_id}/actions", expected=409,
            payload={"action": "review", "expected_revision": 0, "idempotency_key": uid+"stale"}, extra=manager_headers)
        await asyncio.to_thread(call, "POST", f"/orders/{order_id}/moysklad", expected=400)
        report["checks"].extend(["manager_flow_db_each_step", "duplicate_finalize", "duplicate_actions", "stale_rejected", "external_guard"])
        report.update(draft_id=draft_id, order_id=order_id)
        tid = int(uuid4().hex[:12], 16)
        from app.bot.client_bot import create_dispatcher
        client_dp = create_dispatcher()
        responses = []
        async def capture(text, **kwargs):
            responses.append(text)
        client_handler = client_dp.message.handlers[0].callback
        for mid, text in enumerate(("/start", "/request", "MARV007; 1", uid+"-client@example.invalid", "/send"), 1):
            message = SimpleNamespace(chat=SimpleNamespace(type="private"), from_user=SimpleNamespace(id=tid, full_name="RC synthetic client"),
                message_id=mid, text=text, contact=None, answer=capture)
            await client_handler(message)
        import re
        client_draft_id = int(re.search(r"№(\d+)", responses[-1]).group(1))
        await client_handler(message)
        assert responses[-1] == responses[-2]
        status = await ClientChannel().handle(tid, 6, f"/status {client_draft_id}")
        assert "менеджер проверяет" in status
        assert "не найдена" in await ClientChannel().handle(tid+1, 1, f"/status {client_draft_id}")
        visible = await asyncio.to_thread(call, "GET", f"/draft-orders/{client_draft_id}")
        assert visible["source"] == "telegram" and visible["customer_type"] == "unknown" and visible["total_minor"] is None
        report["client_draft_id"] = client_draft_id
        report["checks"].extend(["client_fake_transport_real_staging_db", "client_duplicate_update", "client_ownership", "manager_visibility"])
        from app.bot import telegram_bot as bot
        message = SimpleNamespace(from_user=SimpleNamespace(id=manager.telegram_id), chat=SimpleNamespace(type="private"), answer=AsyncMock(), text=f"/order {order_id}")
        for label, operation in [("manager_menu_handler", lambda: bot.start_handler(message)), ("order_list_handler", lambda: bot.show_order_list(message,"all",0)), ("order_card_handler", lambda: bot.find_order(message))]:
            start = perf_counter()
            await operation()
            report["metrics_ms"][label] = round((perf_counter()-start)*1000,2)
        report["manager_transport"] = "fake send/ack; application and staging DB real"
        async with async_session() as session:
            assert await session.scalar(select(func.count()).select_from(ExternalOperation)) == 0
            assert await session.scalar(select(func.count()).select_from(Shipment).where(Shipment.external_id.is_not(None))) == 0
        await engine.dispose()
    try:
        asyncio.run(scenario())
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure_type"] = type(error).__name__
        if isinstance(error, AssertionError):
            report["assertion"] = str(error)
    Path(".staging-artifacts/release_candidate_e2e.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
