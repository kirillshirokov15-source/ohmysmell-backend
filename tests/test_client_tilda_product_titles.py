"""Customer-visible Tilda titles are read from the linked order, never MoySklad."""
import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from app.models.order_desk import DeskMessage
from app.integrations.tilda import normalize
from app.services.desk_status import public_summary, tilda_product_lines
from app.services import order_desk
from tests.test_tilda_sku_review import database, catalog


def _desk():
    return SimpleNamespace(draft_id=42, stage="new", order_id=None)


def _draft(names, *, source="website"):
    items = [SimpleNamespace(id=i, raw_product_text=name, product_name="PRIVATE MOYSKLAD",
                             qty=qty) for i, (name, qty) in enumerate(names, 1)]
    return SimpleNamespace(status="needs_review", source=source, items=items)


def test_tilda_names_quantities_without_internal_catalog_or_prices():
    draft = _draft([("Maison Francis Kurkdjian Baccarat Rouge 540, 70 ml", 2),
                    ("Набор шампунь и кондиционер", 1)])
    summary = public_summary(_desk(), draft)
    assert "Ваши товары (по заказу на сайте):" in summary
    assert "• Maison Francis Kurkdjian Baccarat Rouge 540, 70 ml – 2 шт." in summary
    assert "• Набор шампунь и кондиционер – 1 шт." in summary
    assert "PRIVATE MOYSKLAD" not in summary
    assert "₽" not in summary
    assert "catalog" not in summary


def test_confirmed_order_keeps_original_site_names_and_current_status():
    draft = _draft([("Название Tilda – оригинал", 3)])
    order = SimpleNamespace(id=111, status="new", fulfillment_status="assembling",
                            payment_status="unpaid", delivery_status="pending",
                            delivery_method="unselected", delivery_reference=None, total=123450)
    desk = SimpleNamespace(draft_id=42, order_id=111, stage="new")
    summary = public_summary(desk, draft, order)
    assert "Собираем ваш заказ" in summary
    assert "Сумма: 1 234,50 ₽" in summary
    assert "Название Tilda – оригинал – 3 шт." in summary
    assert "Название Tilda" not in public_summary(desk, draft, order, include_items=False)


def test_customer_scope_no_nonwebsite_details_or_unrequested_items():
    draft = _draft([("Internal wholesale product name", 12)], source="email")
    assert "Internal wholesale" not in public_summary(_desk(), draft)
    draft.source = "website"
    assert "Internal wholesale" not in public_summary(_desk(), draft, include_items=False)


def test_long_tilda_titles_are_safe_for_telegram():
    names = [("💄" * 250 + "\n  Приходит на сайте", 9) for _ in range(70)]
    draft = _draft(names)
    lines = tilda_product_lines(draft)
    summary = public_summary(_desk(), draft)
    assert len(summary.encode("utf-16-le")) // 2 < 4096
    assert all("\n" not in name for name in lines[2:])
    assert "ещё " in summary
    assert "• " in summary


def test_real_local_tilda_to_bot_status_order_and_ownership(database, catalog, monkeypatch):
    """Local SQLite transaction+outbox, no Telegram network or external writes."""
    catalog.return_value = [
        {"id": "ms-dsd", "article": "DSD020", "name": "PRIVATE MOYSKLAD A",
         "salePrices": [{"value": 100}], "stock": 0},
        {"id": "ms-set", "article": "SET001", "name": "PRIVATE MOYSKLAD B",
         "salePrices": [{"value": 100}], "stock": 0},
    ]
    monkeypatch.setenv("CLIENT_TELEGRAM_BOT_USERNAME", "synthetic_client_bot")
    monkeypatch.setenv("ORDER_DESK_SEND_ENABLED", "false")

    async def run():
        payload = normalize({
            "tranid": "synthetic-client-titles-20261014",
            "Name": "TEST OMS only",
            "Email": "synthetic@example.invalid",
            "Phone": "+70000000000",
            "amount": "37.00",
            "currency": "RUB",
            "products": [
                {"externalid": "DSD020", "name": "Tilda Парфюм 💄", "quantity": 2, "price": "10.00"},
                {"externalid": "SET001", "name": "Tilda Набор 5 предметов", "quantity": 1, "price": "17.00"},
            ]})
        did = (await order_desk.TildaIntake().submit(payload))["draft_id"]
        link = await order_desk.DeskService().issue_link(did)
        await order_desk.DeskService().client(9000101, 1,
                                             "/start " + link["deep_link"].split("start=")[1])
        await order_desk.DeskService().client(9000101, 2, f"/status {did}")
        await order_desk.DeskService().client(9000101, 3, "📦 Мои заказы")
        await order_desk.DeskService().client(9000202, 1, f"/status {did}")
        async with database() as session:
            msgs = (await session.scalars(select(DeskMessage).where(
                DeskMessage.direction == "to_customer").order_by(DeskMessage.id))).all()
            mine = [m for m in msgs if m.destination == 9000101]
            others = [m for m in msgs if m.destination == 9000202]
            assert len(mine) == 3 and len(others) == 1
            for msg in mine[:2]:
                assert "Tilda Парфюм 💄 – 2 шт." in msg.body
                assert "Tilda Набор 5 предметов – 1 шт." in msg.body
                assert "PRIVATE MOYSKLAD" not in msg.body
            assert "Tilda Парфюм" not in mine[2].body
            assert "Выберите заказ" in mine[2].body
            assert all("Tilda Парфюм" not in m.body for m in others)
            assert "не найдена среди ваших" in others[0].body
    asyncio.run(run())
