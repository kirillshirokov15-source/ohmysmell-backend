"""Buying transactions share the existing supply catalog and never customer data."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, delete, func
from fastapi import HTTPException
from app.models.buying import BuyingSupplier, BuyingProduct, BuyingOffer, BuyingImport, BuyingPriceHistory, BuyingCart, BuyingCheckout, BuyingPurchase, BuyingReply, BuyingEvent
from app.models.supply import Supplier, SupplierOffer, ProductSupply
from app.services.buying_excel import normalize
from app.services.checkout_service import transaction_lock
from app.services.buying_adapters import FakeSupplierEmailSender, FakeProcurementAdapter
from app.services.fx import convert_minor


def now():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def lock(session):
    await transaction_lock(session, "buying-workspace")


def fail(message, status=409):
    raise HTTPException(status, detail=message)


async def supplier_get(session, identifier):
    supplier = await session.get(Supplier, identifier)
    config = await session.get(BuyingSupplier, identifier)
    if not supplier or supplier.supplier_type != "external_wholesaler" or not config:
        fail("Buying supplier not found", 404)
    return supplier, config


def offer_view(offer, metadata, supplier):
    rub = offer.purchase_price_minor if offer.currency_code == "RUB" else (
        convert_minor(offer.purchase_price_minor, offer.current_fx_rate_to_rub) if offer.current_fx_rate_to_rub else None)
    return dict(id=offer.id, product_id=offer.product_id, supplier_id=supplier.id, supplier_name=supplier.name,
        name=metadata.name, supplier_sku=offer.supplier_sku, purchase_price_minor=offer.purchase_price_minor,
        currency_code=offer.currency_code, approximate_rub_minor=rub, approximate=offer.currency_code != "RUB",
        fx_source=offer.fx_source, fx_rate_date=str(offer.fx_rate_date) if offer.fx_rate_date else None,
        fx_rate_to_rub=str(offer.current_fx_rate_to_rub) if offer.current_fx_rate_to_rub else None,
        price_list_updated_at=offer.valid_at.isoformat() if offer.valid_at else None,
        availability=offer.availability, active=offer.active and supplier.status == "active",
        moysklad_match_state="local" if offer.product_id.startswith("buying-") else "mapped")


def offer_query():
    return select(SupplierOffer, BuyingOffer, Supplier).join(BuyingOffer, BuyingOffer.offer_id == SupplierOffer.id).join(Supplier, Supplier.id == SupplierOffer.supplier_id)


async def preview_import(session, supplier_id, filename, rows):
    supplier, config = await supplier_get(session, supplier_id)
    known = {meta.mapping_key: offer for offer, meta, _ in (await session.execute(
        offer_query().where(Supplier.id == supplier_id))).all()}
    names = {normalize(row["name"]) for row in rows if not row["error"]}
    products = (await session.scalars(select(BuyingProduct).where(BuyingProduct.normalized_name.in_(names)))).all()
    by_name = {}
    for product in products:
        by_name.setdefault(product.normalized_name, []).append(product.product_id)
    counts = dict(total=len(rows), valid=0, invalid=0, new=0, changed=0, unchanged=0, ambiguous=0)
    for row in rows:
        if row["error"]:
            counts["invalid"] += 1
            continue
        counts["valid"] += 1
        old = known.get(row["mapping_key"])
        candidates = by_name.get(normalize(row["name"]), [])
        row.update(offer_id=old.id if old else None, old_price_minor=old.purchase_price_minor if old else None,
                   product_id=old.product_id if old else (candidates[0] if len(candidates) == 1 else None), candidates=candidates)
        row["state"] = "changed" if old and old.purchase_price_minor != row["price_minor"] else "unchanged" if old else "ambiguous" if len(candidates) > 1 else "new"
        counts[row["state"]] += 1
    record = BuyingImport(id=str(uuid4()), supplier_id=supplier_id, filename=filename, currency=config.currency,
                          parser_version=config.parser["version"], rows=rows, counts=counts)
    session.add(record)
    await session.flush()
    return record


async def confirm_import(session, supplier_id, import_id, mappings=None):
    await lock(session)
    _, config = await supplier_get(session, supplier_id)
    record = await session.get(BuyingImport, import_id, with_for_update=True)
    if not record or record.supplier_id != supplier_id:
        fail("Import not found", 404)
    if record.imported_at:
        return record
    mappings = mappings or {}
    if record.counts["invalid"]:
        fail("Resolve invalid or ambiguous rows and upload a new preview")
    rows = [dict(row) for row in record.rows]
    for row in rows:
        if row.get("state") == "ambiguous":
            chosen = mappings.get(str(row["row"]))
            if chosen not in row["candidates"]:
                fail("Select a listed product candidate for each ambiguous row")
            row["product_id"] = chosen
    for row in rows:
        existing = (await session.execute(offer_query().where(Supplier.id == supplier_id, BuyingOffer.mapping_key == row["mapping_key"]))).first()
        if (existing[0].id if existing else None) != row["offer_id"] or (existing and existing[0].purchase_price_minor != row["old_price_minor"]):
            fail("Catalog changed since preview; upload again")
        if existing:
            offer, meta, _ = existing
            if offer.currency_code != config.currency:
                fail("Offer currency mismatch")
            if offer.purchase_price_minor != row["price_minor"]:
                session.add(BuyingPriceHistory(offer_id=offer.id, old_price_minor=offer.purchase_price_minor,
                    new_price_minor=row["price_minor"], currency=offer.currency_code, import_id=record.id))
            offer.purchase_price_minor = row["price_minor"]
            offer.estimated_purchase_cost_rub_minor = None
            meta.name = row["name"]
        else:
            product_id = row["product_id"]
            if not product_id:
                candidates = (await session.scalars(select(BuyingProduct).where(BuyingProduct.normalized_name == normalize(row["name"])))).all()
                if len(candidates) > 1:
                    fail("Product mapping became ambiguous; upload again")
                product_id = candidates[0].product_id if candidates else "buying-" + str(uuid4())
                if not candidates:
                    session.add(ProductSupply(product_id=product_id, source_type="external"))
                    await session.flush()
                    session.add(BuyingProduct(product_id=product_id, name=row["name"], normalized_name=normalize(row["name"])))
                    await session.flush()
            offer = SupplierOffer(product_id=product_id, supplier_id=supplier_id, supplier_sku=row["sku"],
                purchase_price_minor=row["price_minor"], currency_code=config.currency, availability="on_request")
            session.add(offer)
            await session.flush()
            session.add(BuyingOffer(offer_id=offer.id, supplier_id=supplier_id, mapping_key=row["mapping_key"], name=row["name"]))
        offer.valid_at = now()
        await session.flush()
    record.imported_at = now()
    record.rows = rows
    return record


async def cart_view(session):
    rows = (await session.execute(offer_query().add_columns(BuyingCart).join(BuyingCart, BuyingCart.offer_id == SupplierOffer.id).order_by(Supplier.id, SupplierOffer.id))).all()
    return [dict(offer=offer_view(o, m, s), quantity=c.quantity, price_changed_since_added=c.added_price_minor != o.purchase_price_minor,
                 created_at=c.created_at.isoformat(), updated_at=c.updated_at.isoformat()) for o, m, s, c in rows]


async def checkout_preview(session):
    cart = await cart_view(session)
    if not cart:
        fail("Cart is empty")
    suppliers = {s.id: s for s in (await session.scalars(select(Supplier).where(Supplier.id.in_({r['offer']['supplier_id'] for r in cart})))).all()}
    groups = {}
    for row in cart:
        offer = row["offer"]
        if not offer["active"] or offer["availability"] == "unavailable":
            fail("Cart contains inactive/unavailable offers")
        supplier = suppliers[offer["supplier_id"]]
        if not supplier.email:
            fail("Supplier email is missing")
        group = groups.setdefault(supplier.id, dict(supplier_id=supplier.id, supplier_name=supplier.name, recipient=supplier.email,
            items=[], currency=offer["currency_code"], total_minor=0, approximate_rub_minor=0, approximate=offer["approximate"]))
        if group["currency"] != offer["currency_code"]:
            fail("Supplier offers must share one currency")
        group["items"].append(dict(offer_id=offer["id"], product_id=offer["product_id"], name=offer["name"], quantity=row["quantity"],
            unit_price_minor=offer["purchase_price_minor"], approximate_rub_minor=offer["approximate_rub_minor"],
            fx_rate_to_rub=offer["fx_rate_to_rub"], fx_source=offer["fx_source"], fx_rate_date=offer["fx_rate_date"]))
        group["total_minor"] += row["quantity"] * offer["purchase_price_minor"]
        if offer["approximate_rub_minor"] is None:
            group["approximate_rub_minor"] = None
        elif group["approximate_rub_minor"] is not None:
            group["approximate_rub_minor"] += row["quantity"] * offer["approximate_rub_minor"]
    result = list(groups.values())
    for group in result:
        group["email_body"] = "\n".join(f"{i['name']} – {i['quantity']} шт." for i in group["items"])
    return dict(fingerprint=fingerprint(result), suppliers=result)


async def checkout_confirm(session, key, expected, actor):
    await lock(session)
    existing = await session.get(BuyingCheckout, key)
    if existing:
        if existing.fingerprint != expected:
            fail("Idempotency key reused with different preview")
        return existing.purchase_ids
    preview = await checkout_preview(session)
    if preview["fingerprint"] != expected:
        fail("Cart or prices changed; review a new preview")
    ids = []
    for snapshot in preview["suppliers"]:
        purchase = BuyingPurchase(supplier_id=snapshot["supplier_id"], snapshot=snapshot, status="draft", send_state="disabled")
        session.add(purchase)
        await session.flush()
        ids.append(purchase.id)
        session.add(BuyingEvent(purchase_id=purchase.id, action="created", actor=actor))
    session.add(BuyingCheckout(key=key, fingerprint=expected, purchase_ids=ids))
    await session.execute(delete(BuyingCart))
    return ids


async def purchase_get(session, identifier):
    purchase = await session.get(BuyingPurchase, identifier)
    if not purchase:
        fail("Purchase not found", 404)
    return purchase


def purchase_view(p):
    return dict(id=p.id, number=f"B-{p.id:06d}", status=p.status, send_state=p.send_state,
        created_at=p.created_at, updated_at=p.updated_at, sent_at=p.sent_at, received_at=p.received_at,
        message_id=p.message_id, thread_id=p.thread_id, external_ids=p.external_ids, item_count=len(p.snapshot["items"]), **p.snapshot)


async def simulate_send(session, identifier, actor):
    await lock(session)
    p = await purchase_get(session, identifier)
    if p.send_state == "simulated":
        return p
    if p.status != "draft":
        fail("Purchase cannot be sent")
    sent = await FakeSupplierEmailSender().send(key=str(p.id), recipient=p.snapshot["recipient"], body=p.snapshot["email_body"])
    p.message_id, p.thread_id = sent.message_id, sent.thread_id
    p.send_state, p.status, p.sent_at = "simulated", "sent", now()
    p.external_ids = await FakeProcurementAdapter().order(p.id, p.supplier_id)
    session.add(BuyingEvent(purchase_id=p.id, action="email_simulated", actor=actor))
    return p


async def receive(session, identifier, actor):
    await lock(session)
    p = await purchase_get(session, identifier)
    if p.status == "received":
        return p
    if p.status != "sent":
        fail("Only sent purchases can be received")
    p.status, p.received_at = "received", now()
    p.external_ids = {**p.external_ids, **await FakeProcurementAdapter().receipt(p.id)}
    session.add(BuyingEvent(purchase_id=p.id, action="received", actor=actor))
    return p


async def ingest_reply(session, *, message_id, thread_id, sender, received_at, subject, body, attachments=None):
    """Provider entry point: only exact purchase thread AND supplier sender match."""
    await lock(session)
    existing = await session.get(BuyingReply, message_id)
    if existing:
        return existing
    pairs = (await session.execute(select(BuyingPurchase, Supplier).join(Supplier).where(BuyingPurchase.thread_id == thread_id))).all()
    matches = [(p,s) for p,s in pairs if s.email and s.email.casefold() == sender.casefold() and message_id != p.message_id]
    if len(matches) != 1:
        return None
    p, supplier = matches[0]
    reply = BuyingReply(message_id=message_id, thread_id=thread_id, purchase_id=p.id, supplier_id=supplier.id,
        received_at=received_at, subject=subject[:500], body=body[:20000], attachments=(attachments or [])[:50])
    session.add(reply)
    session.add(BuyingEvent(purchase_id=p.id, action="supplier_reply", actor="gmail-readonly"))
    await session.flush()
    return reply
