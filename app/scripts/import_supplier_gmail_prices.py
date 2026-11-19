"""Review and load the latest verified price workbook for each Gmail sender."""
import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import json
from pathlib import PurePath
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.session import async_session
from app.integrations.email.secret_files import prepare_secret_files
from app.integrations.email.supplier_gmail import SupplierGmailProvider
from app.integrations.email.supplier_workbooks import MAX_BYTES, TITLE, currencies, find_price_sheet, safe_text
from app.integrations.moysklad.client import MoySkladClient
from app.models.buying import BuyingImport, BuyingOffer, BuyingPriceHistory, BuyingProduct, BuyingSupplier, IntegrationHealth
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


PRICE_STATEMENT = re.compile(
    r"(?:\bцен(?:а|ы|ники)\s+(?:(?:указан[аы]|выражен[аы]|приведен[аы]|приведён[аы]|представлен[аы])\s+)?в\s+"
    r"|\bprices?\s+(?:(?:are|listed)\s+){0,2}in\s+)([^\n.!?]{1,32})", re.I
)


def message_currency(provider, payload, subject, filename):
    declared = set()
    for value in (subject, PurePath(filename).stem):
        if TITLE.search(value):
            declared |= currencies(value)
    body = provider._body_text(payload)[:8000]
    # Stop at commonly quoted message / signature separators.
    body = re.split(r"(?im)^(?:>|--\s*$|отправлено:|from:|[- ]{2,}original message)", body, maxsplit=1)[0]
    for match in PRICE_STATEMENT.finditer(body):
        declared |= currencies(match[1])
    return next(iter(declared)) if len(declared) == 1 and declared <= {"RUB", "USD"} else None


def scan(configured, overrides, default_currency="USD", *, known_only=False):
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
            except Exception:
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
            if known_only and sender not in configured:
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
                ref = digest(item["id"] + ":" + part.get("partId", "") + ":" + filename)[:12]
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
                    source_key = digest("supplier-price:row-max-v2:" + provider.account + ":" + sender + ":" + hashlib.sha256(raw).hexdigest())
                    fallback = (overrides.get(sender_key) or message_currency(provider, payload, subject, filename)
                                or configured.get(sender) or default_currency)
                    sheet = find_price_sheet(raw, filename, subject=subject, verified_currency=fallback)
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
                # On the same day prefer the larger sheet if the sender splits a full list into smaller extracts.
                score = (received.date(), len(sheet.rows), received)
                if current is None or score > current[0]:
                    picked[sender_key] = (score, entry)
        cursor = page.get("nextPageToken")
        if not cursor:
            break
    result = [entry for _, entry in picked.values()]
    for entry in result:
        p = entry["sheet"]
        emit("PRICE_PLAN", {"ref": entry["ref"], "sender_key": entry["sender_key"], "supplier": entry["name"],
                            "date": entry["received"].isoformat(), "ext": PurePath(entry["filename"]).suffix.lower(),
                            "sheet": p.sheet, "name": column_name(p.name_column),
                            "prices": [column_name(col) for col in p.price_columns],
                            "sku": column_name(p.sku_column) if p.sku_column is not None else None,
                            "currency": p.currency, "currency_evidence": p.currency_evidence, "rows": len(p.rows),
                            "duplicate_or_conflict_rows": p.skipped})
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
        else:
            supplier = Supplier(supplier_type="external_wholesaler", name=entry["name"], email=entry["sender"])
            session.add(supplier)
            await session.flush()
        config = await session.get(BuyingSupplier, supplier.id)
        if not config:
            parser = dict(version="columns-v1", sheet=p.sheet, first_row=p.first_row, name_column=column_name(p.name_column),
                          price_column=column_name(p.price_column),
                          sku_column=column_name(p.sku_column) if p.sku_column is not None else None)
            session.add(BuyingSupplier(supplier_id=supplier.id, currency=p.currency, parser=parser))
        old_rows = (await session.execute(select(
            BuyingOffer.mapping_key, BuyingOffer.name, SupplierOffer.id, SupplierOffer.product_id,
            SupplierOffer.purchase_price_minor, SupplierOffer.currency_code, SupplierOffer.valid_at,
        ).join(SupplierOffer, BuyingOffer.offer_id == SupplierOffer.id).where(BuyingOffer.supplier_id == supplier.id))).all()
        existing = {row.mapping_key: row for row in old_rows}
        latest = max((row.valid_at for row in old_rows if row.valid_at), default=None)
        if latest and latest > entry["received"]:
            return {"status": "older_than_catalog", "supplier_id": supplier.id}

        rows, products, offers, metadata = p.rows, {}, [], []
        price_updates, name_updates, history = [], [], []
        matched = 0
        import_id = str(uuid4())
        for row in rows:
            previous = existing.get(row["mapping_key"])
            currency = row["currency_code"]
            if previous is not None:
                changed = previous.purchase_price_minor != row["price_minor"] or previous.currency_code != currency
                row.update(product_id=previous.product_id, offer_id=previous.id, old_price_minor=previous.purchase_price_minor,
                           old_currency_code=previous.currency_code, state="changed" if changed else "unchanged")
                price_updates.append(dict(record_id=previous.id, purchase_price_minor=row["price_minor"],
                                          currency_code=currency, valid_at=entry["received"],
                                          estimated_purchase_cost_rub_minor=None))
                if previous.name != row["name"]:
                    name_updates.append(dict(record_id=previous.id, name=row["name"]))
                if changed:
                    history.append(dict(offer_id=previous.id, old_price_minor=previous.purchase_price_minor,
                                        new_price_minor=row["price_minor"], currency=currency, import_id=import_id))
                continue
            matches = index.get(normalize(row["sku"]), set()) if row["sku"] else set()
            product_id = next(iter(matches)) if len(matches) == 1 else "buying-" + str(uuid4())
            matched += len(matches) == 1
            products[product_id] = dict(product_id=product_id, name=row["name"], normalized_name=normalize(row["name"]))
            offers.append(dict(product_id=product_id, supplier_id=supplier.id, supplier_sku=row["sku"],
                               purchase_price_minor=row["price_minor"], currency_code=currency,
                               availability="on_request", valid_at=entry["received"]))
            metadata.append(dict(supplier_id=supplier.id, mapping_key=row["mapping_key"], name=row["name"]))
            row["product_id"] = product_id
        for table, key, changes in ((SupplierOffer.__table__, "id", price_updates),
                                    (BuyingOffer.__table__, "offer_id", name_updates)):
            if changes:
                statement = update(table).where(table.c[key] == bindparam("record_id")).values(
                    {field: bindparam(field) for field in changes[0] if field != "record_id"})
                for group in chunks(changes):
                    await session.execute(statement, group)
        for group in chunks(list(products.values())):
            await session.execute(pg_insert(ProductSupply).on_conflict_do_nothing(index_elements=["product_id"]),
                                  [dict(product_id=v["product_id"], source_type="external") for v in group])
            await session.execute(pg_insert(BuyingProduct).on_conflict_do_nothing(index_elements=["product_id"]), group)
        for start in range(0, len(offers), 500):
            batch = offers[start:start + 500]
            ids = (await session.scalars(insert(SupplierOffer).returning(SupplierOffer.id, sort_by_parameter_order=True), batch)).all()
            await session.execute(insert(BuyingOffer), [dict(meta, offer_id=offer_id) for meta, offer_id in
                                  zip(metadata[start:start + 500], ids, strict=True)])
        session.add(BuyingImport(id=import_id, source_key=entry["source_key"], supplier_id=supplier.id,
                                filename=entry["filename"], currency=p.currency, parser_version="gmail-price-row-max-v2",
                                rows=rows, imported_at=datetime.now(timezone.utc),
                                counts=dict(total=len(rows), valid=len(rows), invalid=0, new=len(offers), changed=len(history),
                                            unchanged=len(price_updates)-len(history), ambiguous=0, skipped=p.skipped)))
        await session.flush()
        for group in chunks(history):
            await session.execute(insert(BuyingPriceHistory), group)
        sample = next((row for row in rows if "jacques bogart one man show" in row["name"].casefold()
                       and re.search(r"(?<!\d)3\s*(?:ml|мл)\b", row["name"], re.I)
                       and "бальзам" in row["name"].casefold()), None)
        if sample and "прокофьев" in supplier.name.casefold():
            emit("PRICE_ROW_CHECK", {"supplier_id": supplier.id, "old_minor": sample.get("old_price_minor"),
                                     "old_currency": sample.get("old_currency_code"), "new_minor": sample["price_minor"],
                                     "currency": sample["currency_code"], "column": column_name(sample["price_column"])})
        return {"status": "imported", "rows": len(rows), "new": len(offers), "changed": len(history),
                "catalog_sku_matches": matched, "supplier_id": supplier.id}


async def refresh(approved=None, overrides=None, default_currency="USD", *, known_only=False):
    async with async_session() as session:
        values = (await session.execute(select(Supplier.email, BuyingSupplier.currency).join(
            BuyingSupplier, BuyingSupplier.supplier_id == Supplier.id).where(Supplier.status == "active"))).all()
    configured = {email.casefold(): currency for email, currency in values if email}
    plan = await asyncio.to_thread(scan, configured, overrides or {}, default_currency, known_only=known_only)
    if not known_only and not approved:
        return {}
    try:
        products = await asyncio.to_thread(MoySkladClient().get_products)
    except Exception as error:
        emit("PRICE_IMPORT_STOP", {"catalog_error": type(error).__name__})
        return {"catalog_error": 1}
    index = defaultdict(set)
    for item in products:
        if not item.get("archived"):
            for field in ("article", "code"):
                sku = item.get(field)
                if sku:
                    index[normalize(str(sku))].add(item["id"])
    totals = Counter()
    supplier_ids = []
    for entry in plan:
        if (not known_only and entry["sender_key"] not in approved) or not entry["sheet"].currency:
            continue
        try:
            result = await publish(entry, index)
        except Exception as error:
            result = {"status": "database_error", "type": type(error).__name__,
                      "sqlstate": getattr(getattr(error, "orig", None), "sqlstate", None)}
        totals[result["status"]] += 1
        if result["status"] in {"imported", "already_imported"}:
            totals["offers"] += result["rows"]
            supplier_ids.append(result["supplier_id"])
        emit("PRICE_LOAD", {"ref": entry["ref"], "supplier": entry["name"], **result})
    async with async_session() as session:
        visible = await session.scalar(select(func.count()).select_from(SupplierOffer).join(
            BuyingOffer, BuyingOffer.offer_id == SupplierOffer.id).join(
            Supplier, Supplier.id == SupplierOffer.supplier_id).join(
            BuyingProduct, BuyingProduct.product_id == SupplierOffer.product_id).where(
            Supplier.id.in_(supplier_ids), Supplier.status == "active", SupplierOffer.active.is_(True)))
    totals["visible_catalog_offers"] = visible
    emit("PRICE_IMPORT_DONE", {**totals, "approved": len(approved) if approved is not None else len(configured)})
    return totals


DAILY_STATUS = "supplier_price_import"
MOSCOW = ZoneInfo("Europe/Moscow")


async def run_daily():
    began = datetime.now(timezone.utc)
    local = began.astimezone(MOSCOW)
    scheduled_for = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local < scheduled_for:
        from datetime import timedelta
        scheduled_for -= timedelta(days=1)
    async with async_session() as session:
        status = await session.get(IntegrationHealth, DAILY_STATUS)
    if status:
        elapsed = (began - status.checked_at).total_seconds()
        if (status.status == "success" and status.checked_at >= scheduled_for
                or status.status != "success" and elapsed < 1800):
            return
    emit("PRICE_DAILY_STARTED", {"scheduled_for": scheduled_for.isoformat()})
    try:
        totals = await refresh(known_only=True)
        ok = not (totals.get("database_error") or totals.get("catalog_error"))
    except Exception as error:
        emit("PRICE_DAILY_ERROR", {"type": type(error).__name__})
        ok = False
    async with async_session() as session, session.begin():
        row = await session.get(IntegrationHealth, DAILY_STATUS)
        if row is None:
            row = IntegrationHealth(name=DAILY_STATUS)
            session.add(row)
        row.status = "success" if ok else "error"
        row.checked_at = datetime.now(timezone.utc)
        row.retry_after_seconds = 0 if ok else 1800
    emit("PRICE_DAILY_DONE", {"result": "success" if ok else "retry", "next_after_moscow": "09:00"})


async def main(approved, overrides, default_currency, known_only):
    prepare_secret_files("supplier")
    await refresh(approved, overrides, default_currency, known_only=known_only)



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-senders", default="")
    parser.add_argument("--refresh-configured", action="store_true")
    parser.add_argument("--currencies", default="{}")
    parser.add_argument("--default-currency", choices=("USD", "RUB"), default="USD",
                        help="Fallback when neither the workbook nor its sender specifies the currency")
    args = parser.parse_args()
    asyncio.run(main(set(filter(None, args.apply_senders.split(","))), json.loads(args.currencies), args.default_currency, args.refresh_configured))
