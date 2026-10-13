"""Tilda-only review: declared site prices, exact SKU, no external writes."""
import json
import math
from dataclasses import asdict

from app.integrations.tilda import digest
from app.services.product_matching_service import ProductMatchingService


REVIEW_NOTE = "Цена заявлена сайтом. Требуется подтверждение менеджером."
ITEM_FIELDS = ("product_id", "product_name", "article", "match_status", "candidates",
               "qty", "quantity_confidence", "price", "item_total")


def amounts(payload):
    problems = set(payload.get("problems", []))
    subtotal = 0
    for row in payload["items"]:
        price, qty = row.get("price_minor"), row.get("qty")
        if type(qty) is not int or not 1 <= qty <= 10000:
            problems.add("invalid_quantity")
        if type(price) is not int or not 0 < price <= 100_000_000_000:
            problems.add("invalid_reported_price")
        if type(price) is int and type(qty) is int:
            subtotal += price * qty
            if "reported_item_total_minor" in row and row["reported_item_total_minor"] != price * qty:
                problems.add("item_total_mismatch")
    discount, total = payload.get("discount"), payload.get("reported_total")
    if type(discount) is not int or not 0 <= discount <= subtotal:
        problems.add("invalid_discount")
    elif discount:
        problems.add("discount_requires_agreement")
    if type(total) is not int or total <= 0:
        problems.add("invalid_total")
    if type(discount) is not int or total != subtotal - discount:
        problems.add("total_mismatch")
    if payload.get("currency") != "RUB":
        problems.add("unsupported_currency")
    return sorted(problems), total if not problems else None


async def review(payload, manual_matches=None):
    problems, total = amounts(payload)
    try:
        matcher = ProductMatchingService()
        ttl = matcher.async_gateway.product_cache.ttl_seconds
        if not math.isfinite(ttl) or not 0 < ttl <= 300:
            raise ValueError("Tilda catalog TTL must be in (0, 300] seconds")
        # Gateway refreshes an expired snapshot; it never falls back to stale data.
        products = await matcher.products_async()
    except Exception:
        products = []
        problems.append("catalog_unavailable")
    # Manual bindings refer to original source indices and are revalidated on
    # every review/confirmation against the live MoySklad catalog.
    bindings = manual_matches or {}
    if not isinstance(bindings, dict):
        raise ValueError("Invalid manual bindings")
    known = {p["id"]: p for p in products if p.get("id") and not p.get("archived")}
    if any(not isinstance(k, str) or not k.isdecimal() or int(k) >= len(payload["items"])
           or not isinstance(v, str) for k, v in bindings.items()):
        raise ValueError("Invalid manual bindings")
    rows = []
    for index, row in enumerate(payload["items"]):
        match = ProductMatchingService.match_sku(products, row["externalid"], row["qty"])
        product = match.product or {}
        match_status = match.status
        if str(index) in bindings:
            product = known.get(bindings[str(index)]) or {}
            if product:
                match_status = "matched"
            else:
                match_status = "not_found"
                problems.append("sku_manual_invalid")
        elif match.status != "matched":
            problems.append("sku_" + match.status)
        price, qty = row.get("price_minor"), row["qty"]
        valid_price = type(price) is int and 0 < price <= 100_000_000_000
        valid_qty = type(qty) is int and 1 <= qty <= 10000
        rows.append(dict(product_id=product.get("id"), product_name=product.get("name") or row["name"],
            article=product.get("article"), match_status=match_status,
            candidates=[asdict(c) for c in match.candidates], qty=qty,
            quantity_confidence="confirmed" if valid_qty else "uncertain",
            price=price if valid_price else None,
            item_total=price * qty if valid_price and valid_qty else None))
    problems = sorted(set(problems))
    return rows, None if problems else total, problems


def source_rows(draft, desk, inbound):
    """Bind persisted IDs to immutable source indexes, validating legacy ordering."""
    if not inbound or inbound.external_message_id != "tilda:" + desk.external_id:
        raise ValueError("Исходный Webhook не найден.")
    payload = json.loads(inbound.body_text)
    if payload.get("external_id") != desk.external_id or digest(payload) != desk.payload_hash:
        raise ValueError("Исходный Webhook изменён; требуется ручная проверка.")
    rows = payload["items"]
    items = sorted(draft.items, key=lambda i: i.id)
    if len(items) != len(rows):
        raise ValueError("Состав позиций изменён; требуется ручная проверка.")
    bindings = (draft.contact_details or {}).get("tilda_item_sources")
    if bindings is None:
        # Old intake inserted rows in webhook order. Verify all original names,
        # quantities and identifiers before adopting that legacy order.
        if (draft.contact_details or {}).get("external_products") != [r["externalid"] for r in rows]:
            raise ValueError("Нельзя восстановить привязку SKU.")
        if any(i.raw_product_text != r["name"] or i.qty != r["qty"] for i, r in zip(items, rows)):
            raise ValueError("Неоднозначная привязка исходных позиций.")
        bindings = {str(i.id): n for n, i in enumerate(items)}
    if (set(bindings) != {str(i.id) for i in items}
            or any(type(n) is not int for n in bindings.values())
            or sorted(bindings.values()) != list(range(len(rows)))):
        raise ValueError("Некорректная привязка исходных позиций.")
    ordered = sorted(items, key=lambda i: bindings[str(i.id)])
    return payload, ordered, bindings


def apply_review(draft, rows, total, problems, items=None):
    for item, row in zip(items if items is not None else draft.items, rows, strict=True):
        for key in ITEM_FIELDS:
            setattr(item, key, row[key])
    draft.total = total
    draft.review_notes = ", ".join(problems) or REVIEW_NOTE
    draft.status = "needs_review"
    draft.contact_details = {**(draft.contact_details or {}), "tilda_review_approved": False,
                             "tilda_review_problems": problems, "tilda_price_source": "webhook"}


def confirmation_problems(draft, payload, items):
    problems, total = amounts(payload)
    for item, row in zip(items, payload["items"], strict=True):
        if (not item.product_id or item.match_status != "matched"
                or item.quantity_confidence != "confirmed" or item.qty != row["qty"]
                or type(item.price) is not int or item.price <= 0 or item.price != row["price_minor"]
                or item.item_total != item.price * item.qty):
            problems.append("item_review_required")
    if draft.total != total or total is None:
        problems.append("total_review_required")
    problems.extend((draft.contact_details or {}).get("tilda_review_problems", []))
    return sorted(set(problems))
