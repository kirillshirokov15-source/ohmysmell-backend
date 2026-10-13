"""Safe X receipt sequence: a single counterparty and no fake stock writes."""
from app.services.x_warehouse_preview import x_counterparty_matches, real_moysklad_id
from app.services.order_source_service import allocation_ready


def test_counterparty_search_only_exact_name_and_valid_uuid():
    row = dict(id="887acc7e-1335-11f1-0a80-0d620011c941", name="Склад Х", archived=False)
    candidates = x_counterparty_matches([
        row, row, dict(id="aa", name="Склад Х", archived=False),
        dict(id="cb38c341-1111-1111-1111-111111111111", name="Мой склад Х", archived=False),
        dict(id="cb38c341-1111-1111-1111-222222222222", name="Склад Х", archived=True)])
    assert len(candidates) == 1 and candidates[0]["id"] == row["id"]
    assert real_moysklad_id(candidates[0]["id"])


def test_two_distinct_x_counterparties_not_silently_deduplicated():
    rows = [
        dict(id="00000000-0000-4000-8000-000000000001", name="Склад Х"),
        dict(id="00000000-0000-4000-8000-000000000002", name="Склад X"),
    ]
    assert len(x_counterparty_matches(rows)) == 2


def test_confirmed_physical_goods_are_not_automatically_ready_for_shipping():
    from types import SimpleNamespace as S
    allocation = S(managed_source=True, supply_status="received", source_type="partner_x",
                   receipt_reference=None, receipt_simulated=False)
    assert allocation_ready(allocation, manual=True) is False
