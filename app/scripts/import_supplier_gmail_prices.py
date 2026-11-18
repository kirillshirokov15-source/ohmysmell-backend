"""Review and load the latest verified price workbook for each Gmail sender."""
import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import json
import os
from pathlib import PurePath
import re
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.session import async_session
from app.integrations.email.secret_files import prepare_secret_files
from app.integrations.email.supplier_gmail import SupplierGmailProvider
from app.integrations.email.supplier_workbooks import MAX_BYTES, find_price_sheet, safe_text
from app.integrations.moysklad.client import MoySkladClient
from app.models.buying import BuyingImport, BuyingOffer, BuyingProduct, BuyingSupplier
from app.models.supply import ProductSupply, Supplier, SupplierOffer
from app.services import buying
from app.services.buying_excel import normalize


def emit(tag, payload):
    print(tag + " " + json.dumps(payload, default=str, ensure_ascii=False), flush=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def parts(payload, depth=0):
    if depth <= 20:
        yield payload
        for child in payload.get("parts", []):
            yield from parts(child, depth + 1)


def column_name(index):
    name = ""
    while index >= 0:
        index, letter = divmod(index, 26)
        name = chr(65 + letter) + name
        index -= 1
    return name


def scan(configured, approved, overrides):
    provider = SupplierGmailProvider()
    provider.service = provider._build_service()
    api = provider.service.users().messages()
    cursor, picked, reason = None, {}, Counter()
    sizes = Counter()
    candidates = Counter()
    latest = {}
    for _ in range(3):
        page = api.list(userId=provider.user_id, q="newer_than:8d has:attachment {filename:xlsx filename:xls filename:csv} -in:sent -in:drafts",
                        maxResults=30, **({"pageToken": cursor} if cursor else {})).execute()
        for item in page.get("messages", []):
            try:
                message = api.get(userId=provider.user_id, id=item["id"], format="full").execute()
            except Exception as error:
                reason["message_unavailable"] += 1
                continue
            if set(message.get("labelIds", [])) & {"SENT", "DRAFT", "SPAM", "TRASH"}:
                continue
            payload = message.get("payload", {})
            headers = defaultdict(list)
            for header in payload.get("headers", []):
                headers[header["name"].lower()].append(header["value"])
            if len(headers["from"]) != 1:
                continue
            display, sender = parseaddr(headers["from"][0])
            sender = sender.casefold()
            domain = sender.rpartition("@")[2]
            if not sender or sender == provider.account or "@" not in sender:
                continue
            auth = [x for x in headers["authentication-results"] if x.lstrip().startswith("mx.google.com;")]
            if (len(auth) != 1 or not re.search(r"\bdmarc=pass\b", auth[0])
                    or not re.search(r"\bheader\.from=" + re.escape(domain) + r"(?:\s|;|$)", auth[0])):
                reason["sender_not_verified"] += 1
                continue
            received = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc)
            sender_key = digest(sender)[:12]
            subject = headers["subject"][0] if headers["subject"] else ""
            if sender_key in latest and received.date() < latest[sender_key]:
                continue
            for part in parts(payload):
                filename = part.get("filename", "").strip()
                body = part.get("body", {})
                attachment_id = body.get("attachmentId")
                ext = PurePath(filename).suffix.casefold()
                if not attachment_id or ext not in {".xlsx", ".xls", ".csv"}:
                    continue
                source_key = digest("supplier-price:" + provider.account + ":" + item["id"] + ":" + attachment_id)
                ref = digest(item["id"] + ":" + attachment_id)[:12]
                if approved and ref not in approved:
                    continue
                if "/" in filename or "\\" in filename or len(filename) > 255:
                    reason["invalid_filename"] += 1
                    continue
                length = body.get("size", 0)
                if not 0 < length <= MAX_BYTES:
                    reason["attachment_size"] += 1
                    continue
                if candidates[sender_key] >= 5:
                    break
                candidates[sender_key] += 1
                try:
                    encoded = api.attachments().get(userId=provider.user_id, messageId=item["id"], id=attachment_id).execute()["data"]
                    if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                        reason["attachment_size"] += 1
                        continue
                    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                    sheet = find_price_sheet(raw, filename, subject=subject,
                                             verified_currency=overrides.get(sender_key, configured.get(sender)))
                except Exception as error:
                    reason["unreadable_" + type(error).__name__] += 1
                    emit("PRICE_FILE_SKIPPED", {"sender_key": sender_key, "ref": ref, "type": type(error).__name__})
                    continue
                if sheet is None:
                    reason["not_recognized_as_price_list"] += 1
                    continue
                latest.setdefault(sender_key, received.date())
                sizes[ext] += 1
                name = safe_text(display) or sender.partition("@")[0]
                entry = dict(sender=sender, sender_key=sender_key, name=name[:255], received=received, filename=filename,
                             sheet=sheet, source_key=source_key, ref=ref)
                current = picked.get(sender_key)
                # Prefer the larger confirmed sheet when a sender includes different lists on the same day.
                score = (received.date(), sheet.currency is not None, len(sheet.rows), received)
                if current is None or score > current[0]:
                    picked[sender_key] = (score, entry)
        cursor = page.get("nextPageToken")
        if not cursor:
            break
        if approved and approved <= {entry["ref"] for _, entry in picked.values()}:
            break
    result = [entry for _, entry in picked.values()]
    for entry in result:
        p = entry["sheet"]
        emit("PRICE_PLAN", {"ref": entry["ref"], "sender_key": entry["sender_key"], "supplier": entry["name"],
                            "date": entry["received"].isoformat(), "ext": PurePath(entry["filename"]).suffix.lower(),
                            "sheet": p.sheet, "name": column_name(p.name_column), "price": column_name(p.price_column),
                            "sku": column_name(p.sku_column) if p.sku_column is not None else None,
                            "currency": p.currency, "rows": len(p.rows), "duplicate_or_conflict_rows": p.skipped})
    emit("PRICE_SCAN_DONE", {"sources": len(result), "attachment_formats": sizes, "excluded": reason, "more_archive": bool(cursor)})
    return result


def chunks(values, size=500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


async def publish(entry, index):
    p = entry["sheet"]
    async with async_session() as session, session.begin():
        await buying.lock(session)
        old = await session.scalar(select(BuyingImport).where(BuyingImport.source_key == entry["source_key"]))
        if old:
            return {"status": "already_imported", "rows": old.counts["valid"], "supplier_id": old.supplier_id}
        found = (await session.scalars(select(Supplier).where(func.lower(Supplier.email) == entry["sender"],
                                     Supplier.supplier_type == "external_wholesaler"))).all()
        if len(found) > 1 or (found and found[0].status != "active"):
            return {"status": "supplier_requires_review"}
        if found:
            supplier = found[0]
            existing = await session.scalar(select(func.count()).select_from(BuyingOffer).where(
                BuyingOffer.supplier_id == supplier.id))
            if existing:
                return {"status": "existing_offers_require_review", "supplier_id": supplier.id}
        else:
            supplier = Supplier(supplier_type="external_wholesaler", name=entry["name"], email=entry["sender"])
            session.add(supplier)
            await session.flush()
        config = await session.get(BuyingSupplier, supplier.id)
        if config and config.currency != p.currency:
            return {"status": "currency_conflict", "supplier_id": supplier.id}
        if not config:
            parser = dict(version="columns-v1", sheet=p.sheet, first_row=p.first_row, name_column=column_name(p.name_column),
                          price_column=column_name(p.price_column),
                          sku_column=column_name(p.sku_column) if p.sku_column is not None else None)
            session.add(BuyingSupplier(supplier_id=supplier.id, currency=p.currency, parser=parser))
        rows, products, offers, metadata = p.rows, {}, [], []
        matched = 0
        for row in rows:
            matches = index.get(normalize(row["sku"]), set()) if row["sku"] else set()
            product_id = next(iter(matches)) if len(matches) == 1 else "buying-" + str(uuid4())
            matched += len(matches) == 1
            products[product_id] = dict(product_id=product_id, name=row["name"], normalized_name=normalize(row["name"]))
            offers.append(dict(product_id=product_id, supplier_id=supplier.id, supplier_sku=row["sku"],
                               purchase_price_minor=row["price_minor"], currency_code=p.currency,
                               availability="on_request", valid_at=entry["received"]))
            metadata.append(dict(supplier_id=supplier.id, mapping_key=row["mapping_key"], name=row["name"]))
            row["product_id"] = product_id
        for group in chunks(list(products.values())):
            await session.execute(pg_insert(ProductSupply).on_conflict_do_nothing(index_elements=["product_id"]),
                                  [dict(product_id=v["product_id"], source_type="external") for v in group])
            await session.execute(pg_insert(BuyingProduct).on_conflict_do_nothing(index_elements=["product_id"]), group)
        for start in range(0, len(offers), 500):
            batch = offers[start:start + 500]
            ids = (await session.scalars(insert(SupplierOffer).returning(SupplierOffer.id, sort_by_parameter_order=True), batch)).all()
            await session.execute(insert(BuyingOffer), [dict(meta, offer_id=offer_id) for meta, offer_id in
                                  zip(metadata[start:start + 500], ids, strict=True)])
        session.add(BuyingImport(id=str(uuid4()), source_key=entry["source_key"], supplier_id=supplier.id, filename=entry["filename"],
                                currency=p.currency, parser_version="gmail-price-v1", rows=rows, imported_at=datetime.now(timezone.utc),
                                counts=dict(total=len(rows), valid=len(rows), invalid=0, new=len(rows), changed=0, unchanged=0,
                                            ambiguous=0, skipped=p.skipped)))
        return {"status": "imported", "rows": len(rows), "catalog_sku_matches": matched, "supplier_id": supplier.id}


async def main(approved, overrides):
    prepare_secret_files("supplier")
    async with async_session() as session:
        values = (await session.execute(select(Supplier.email, BuyingSupplier.currency).join(
            BuyingSupplier, BuyingSupplier.supplier_id == Supplier.id))).all()
    configured = {email.casefold(): currency for email, currency in values if email}
    plan = await asyncio.to_thread(scan, configured, approved, overrides)
    if not approved:
        return
    try:
        products = await asyncio.to_thread(MoySkladClient().get_products)
    except Exception as error:
        emit("PRICE_IMPORT_STOP", {"catalog_error": type(error).__name__})
        return
    index = defaultdict(set)
    for item in products:
        if not item.get("archived"):
            for field in ("article", "code"):
                sku = item.get(field)
                if sku:
                    index[normalize(str(sku))].add(item["id"])
    totals = Counter()
    for entry in plan:
        if entry["ref"] not in approved or not entry["sheet"].currency:
            continue
        try:
            result = await publish(entry, index)
        except Exception as error:
            result = {"status": "database_error", "type": type(error).__name__,
                      "sqlstate": getattr(getattr(error, "orig", None), "sqlstate", None)}
        totals[result["status"]] += 1
        if result["status"] in {"imported", "already_imported"}:
            totals["offers"] += result["rows"]
        emit("PRICE_LOAD", {"ref": entry["ref"], "supplier": entry["name"], **result})
    emit("PRICE_IMPORT_DONE", {**totals, "approved": len(approved)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", default="")
    parser.add_argument("--currencies", default="{}")
    args = parser.parse_args()
    asyncio.run(main(set(filter(None, args.apply.split(","))), json.loads(args.currencies)))
