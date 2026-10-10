"""Read-only stock hints for the card, never a reservation or source approval."""
from app.services.product_matching_service import ProductMatchingService
from app.services.product_service import ProductService
from app.services.stock_allocation import available_units


async def availability(items):
    wanted = {i.product_id for i in items if i.product_id}
    if not wanted:
        return {}
    try:
        gateway = ProductMatchingService().async_gateway
        stores = await gateway.get_stores()
        active = {s["id"] for s in stores if s.get("id") and not s.get("archived")}
        report = await gateway.get_stock_by_store()
        result = {}
        for row in report["rows"]:
            pid = ProductService._extract_id_from_href(row.get("meta", {}).get("href"))
            if pid not in wanted:
                continue
            values = {}
            for stock in row.get("stockByStore", []):
                sid = ProductService._extract_id_from_href(stock.get("meta", {}).get("href"))
                if sid in active and stock.get("stock") is not None and stock.get("reserve") is not None:
                    if sid in values:
                        return {}  # ambiguous report
                    values[sid] = available_units(stock["stock"], stock["reserve"])
            # Absent warehouse rows are unknown, not evidence of zero stock.
            if active and values.keys() == active:
                if pid in result:
                    return {}
                result[pid] = sum(values.values())
        return result
    except Exception:
        return {}


def stock_label(item, stocks):
    available = stocks.get(item.product_id)
    if available is None:
        return "Наличие: не подтверждено"
    return f"Доступно по текущему отчёту: {available} шт. (без резерва для заказа)"
