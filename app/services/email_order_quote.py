"""Customer-supplied wholesale quote evidence for manager review.

Prices from email are NOT the trusted RUB/MySklad price. This module only
parses persisted inbound message text and line evidence; it performs no IO.
"""
from decimal import Decimal, InvalidOperation
import re
import unicodedata


def _clean(s):
    return " ".join(unicodedata.normalize("NFKC", str(s or "")).casefold().split())


def _price(s):
    raw = str(s or "").strip().replace("\u00a0", "").replace(" ", "")
    raw = re.sub(r"^(?:usd|rub|\$|₽)", "", raw, flags=re.I)
    raw = re.sub(r"(?:usd|rub|\$|₽|руб\.?)$", "", raw, flags=re.I)
    # No silent coercion of malformed numbers or ambiguous locale separators.
    if not re.fullmatch(r"\d{1,11}(?:[.,]\d{1,2})?", raw):
        return None
    try:
        return int(Decimal(raw.replace(",", ".")) * 100)
    except (InvalidOperation, OverflowError):
        return None


def _currency(header):
    value = str(header or "").casefold()
    if "$" in value or "usd" in value or "долл" in value:
        return "USD"
    if "₽" in value or "rub" in value or "руб" in value or re.search(r"цена\s*,\s*р(?:\b|$)", value):
        return "RUB"
    return None


def _head(body):
    for line in (body or "").splitlines():
        cols = [c.strip() for c in re.split(r"\t|\|", line)]
        tokens = [_clean(c) for c in cols]
        if any(t in {"наименование", "название", "товар", "product", "name"} for t in tokens) and any(t in {"количество", "кол-во", "quantity", "qty"} for t in tokens):
            def idx(p):
                return next((n for n, item in enumerate(tokens) if p(item)), None)
            name = idx(lambda t: t in {"наименование", "название", "товар", "product", "name"})
            qty = idx(lambda t: t in {"количество", "кол-во", "quantity", "qty"})
            price = idx(lambda t: t.startswith(("цена", "price", "unit price")))
            subtotal = idx(lambda t: t.startswith(("итого", "сумма", "total", "amount")))
            article = idx(lambda t: t in {"артикул", "sku", "код", "article"})
            return dict(name=name, qty=qty, price=price, subtotal=subtotal,
                        article=article, currency=_currency(cols[price]) if price is not None else None)
    return None


def _stored_items(draft):
    return list(getattr(draft, "items", ()) or ())


def original_body(draft):
    inbound = getattr(draft, "inbound_message", None)
    return getattr(inbound, "body_text", None) or getattr(draft, "body_text", "") or ""


def quote_lines(draft):
    """Summarize previously ingested rows WITHOUT reprocessing Gmail or changing IDs.

    Returns source-provided values, not payable prices. Missing currencies remain unknown.
    """
    body = original_body(draft)
    heading = _head(body)
    items = _stored_items(draft)
    lines = []
    for item in items:
        evidence = str(getattr(item, "quantity_evidence", "") or "")
        article = None
        currency = None
        price = None
        subtotal = None
        if heading and "\t" in evidence:
            raw_row = evidence.split(" | ", 1)[-1]
            cols = [col.strip() for col in raw_row.split("\t")]
            def cell(key):
                i = heading.get(key)
                return cols[i] if i is not None and i < len(cols) else ""
            price = _price(cell("price"))
            subtotal = _price(cell("subtotal"))
            currency = heading["currency"]
            article = cell("article").strip() or None
            if article is None and evidence.startswith("Артикул "):
                article = evidence.split("|", 1)[0].removeprefix("Артикул ").strip() or None
        elif re.fullmatch(r"\s*[a-zа-яё0-9._/-]+\s+\d{1,4}\s*(?:шт\.?|pcs?\.?)\s+\d+(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|\$|USD|RUB)?\s*", evidence, re.I):
            m = re.match(r"\s*\S+\s+\d{1,4}\s*(?:шт\.?|pcs?\.?)\s+(\d+(?:[.,]\d{1,2})?)\s*(.*)$", evidence, re.I)
            if m:
                price = _price(m.group(1))
                currency = _currency(m.group(2))
                article = str(getattr(item, "raw_product_text", "") or "") or None
        if not heading and currency is None and price is None:
            # The original message never claimed a parsable money amount.
            continue
        if price is None and subtotal is None and article is None:
            continue
        qty = int(getattr(item, "qty", 0) or 0)
        discrepancy = bool(price is not None and subtotal is not None and qty > 0
                           and price * qty != subtotal)
        lines.append(dict(
            article=article, unit_minor=price, line_minor=subtotal,
            currency=currency, discrepancy=discrepancy, qty=qty,
        ))
    total = None
    if heading and heading["subtotal"] is not None:
        for row in (body or "").splitlines():
            cells = [c.strip() for c in row.split("\t")]
            if len(cells) < 2:
                continue
            if not any(_clean(c).startswith(("итого", "всего", "total")) for c in cells[:-1]):
                continue
            amount = _price(cells[heading["subtotal"]]) if heading["subtotal"] < len(cells) else None
            if amount is not None:
                total = amount
    calculated = None
    if lines and len(lines) == len(items) and all(
            row["line_minor"] is not None or row["unit_minor"] is not None
            for row in lines):
        calculated = sum(
            row["line_minor"] if row["line_minor"] is not None
            else row["unit_minor"] * row["qty"] for row in lines)
    discrepancies = [idx + 1 for idx, row in enumerate(lines) if row["discrepancy"]]
    if calculated is not None and total is not None and calculated != total:
        discrepancies.append(0)
    return dict(lines=lines, total_minor=total, calculated_minor=calculated,
                currency=heading["currency"] if heading else None,
                discrepancies=discrepancies)


def format_money(minor, currency):
    if minor is None:
        return "не указана"
    numeric = f"{minor / 100:,.2f}".replace(",", " ").replace(".", ",")
    return numeric + " " + (currency or "(валюта не определена)")


def compact_quote(draft):
    quote = quote_lines(draft)
    output = []
    for i, evidence in enumerate(quote["lines"][:3]):
        label = f"Артикул из письма: {evidence['article']} · " if evidence["article"] else ""
        output.append(
            label + "цена клиента: " +
            format_money(evidence["unit_minor"], evidence["currency"])
        )
    if quote["total_minor"] is not None:
        output.append("Итог в письме: " + format_money(
            quote["total_minor"], quote["currency"]))
    if quote["discrepancies"]:
        output.append("⚠️ Арифметика письма не совпадает, нужна проверка.")
    return output


def full_quote(draft):
    result = quote_lines(draft)
    output = []
    for ix, evidence in enumerate(result["lines"], 1):
        label = f"#{ix} Артикул: {evidence['article']} · " if evidence["article"] else f"#{ix} "
        output.append(
            "  " + label + "цена в письме " +
            format_money(evidence["unit_minor"], evidence["currency"]) +
            (" · сумма " + format_money(evidence["line_minor"], evidence["currency"])
             if evidence["line_minor"] is not None else "") +
            (" · ⚠️ сумма строки не совпадает" if evidence["discrepancy"] else "")
        )
    if result["total_minor"] is not None:
        output.append("Итог из письма (не итог CRM): " +
                      format_money(result["total_minor"], result["currency"]))
    if result["discrepancies"]:
        output.append("⚠️ Сумма/цена в письме требует ручной сверки.")
    return output
