"""Explicit Tilda contract. Field paths are configuration, not platform promises."""
import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

DEFAULT_FIELDS = {"external_id": "tranid", "name": "Name", "phone": "Phone",
                  "email": "Email", "comment": "Comments", "items": "products",
                  "total": "amount", "currency": "currency", "discount": "discount"}


class TildaInvalid(ValueError):
    pass


def money(value):
    if isinstance(value, (bool, float)) or value is None:
        raise TildaInvalid("invalid_money")
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
        if not amount.is_finite() or not 0 <= amount <= Decimal("1000000000"):
            raise TildaInvalid("invalid_money")
        minor = amount * 100
        if minor != minor.to_integral_value():
            raise TildaInvalid("fractional_minor_units")
        return int(minor)
    except (InvalidOperation, ValueError):
        raise TildaInvalid("invalid_money") from None


def parse_body(body, content_type):
    try:
        if content_type == "application/json":
            value = json.loads(body, parse_float=Decimal)
        elif content_type == "application/x-www-form-urlencoded":
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=1500)
            value = {}
            for key, item in pairs:
                if key in value:
                    raise TildaInvalid("duplicate_field")
                value[key] = item
            # PHP-style arrays, with bounded contiguous indexes. No generic object injection.
            arrays = {}
            for key in list(value):
                match = re.fullmatch(r"([A-Za-z_]+)\[(\d{1,3})\]\[([A-Za-z_]+)\]", key)
                if match:
                    root, index, field = match.groups()
                    arrays.setdefault(root, {}).setdefault(int(index), {})[field] = value.pop(key)
            for root, rows in arrays.items():
                if root in value or sorted(rows) != list(range(len(rows))):
                    raise TildaInvalid("invalid_array")
                value[root] = [rows[i] for i in range(len(rows))]
        else:
            raise TildaInvalid("unsupported_content_type")
        if not isinstance(value, dict):
            raise TildaInvalid("invalid_object")
        return value
    except (ValueError, UnicodeError, TypeError):
        raise TildaInvalid("invalid_payload") from None


def path_value(payload, path):
    value = payload
    for part in path.split("."):
        if isinstance(value, str):
            try:
                value = json.loads(value, parse_float=Decimal)
            except ValueError:
                return None
        value = value.get(part) if isinstance(value, dict) else None
    return value


def normalize(payload, delivery_key=None):
    fields = {**DEFAULT_FIELDS, **json.loads(os.getenv("TILDA_FIELD_MAP_JSON", "{}"))}
    get = lambda key: path_value(payload, fields[key])
    external = get("external_id") or delivery_key
    if not isinstance(external, (str, int)) or not 1 <= len(str(external)) <= 200:
        raise TildaInvalid("stable_external_id_required")
    items = get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items, parse_float=Decimal)
        except ValueError:
            raise TildaInvalid("invalid_cart") from None
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise TildaInvalid("invalid_cart")
    item_fields = {"id": "externalid", "name": "name", "qty": "quantity", "price": "price",
                   **json.loads(os.getenv("TILDA_ITEM_FIELD_MAP_JSON", "{}"))}
    problems, rows = [], []
    for item in items:
        if not isinstance(item, dict):
            raise TildaInvalid("invalid_item")
        val = lambda key: path_value(item, item_fields[key])
        try:
            qty = Decimal(str(val("qty")))
            if not qty.is_finite() or not 1 <= qty <= 10000 or qty != qty.to_integral_value():
                raise TildaInvalid("invalid_quantity")
        except InvalidOperation:
            raise TildaInvalid("invalid_quantity") from None
        identifier, name = str(val("id") or ""), str(val("name") or "Товар")
        if len(identifier) > 255 or len(name) > 500:
            raise TildaInvalid("invalid_item")
        try:
            price = money(val("price"))
        except TildaInvalid:
            price = None
            problems.append("invalid_reported_price")
        rows.append({"externalid": identifier, "name": name, "qty": int(qty), "price_minor": price})
    currency = str(get("currency") or os.getenv("TILDA_DEFAULT_CURRENCY", "RUB")).upper()
    if currency != "RUB":
        problems.append("unsupported_currency")
    amounts = {}
    for key in ("total", "discount"):
        try:
            amounts[key] = money(get(key) if get(key) not in (None, "") else (0 if key == "discount" else None))
        except TildaInvalid:
            amounts[key] = None
            problems.append("invalid_" + key)
    subtotal = sum(i["price_minor"] * i["qty"] for i in rows) if all(i["price_minor"] is not None for i in rows) else None
    if subtotal is None or amounts["discount"] is None or amounts["total"] != subtotal - amounts["discount"]:
        problems.append("total_mismatch")
    result = {"external_id": str(external), "items": rows, "currency": currency,
              "reported_total": amounts["total"], "discount": amounts["discount"], "problems": sorted(set(problems))}
    for key, limit in (("name", 255), ("phone", 100), ("email", 320), ("comment", 2000)):
        value = get(key)
        if value is not None and not isinstance(value, str):
            raise TildaInvalid("invalid_contact")
        result[key] = (value or "").strip()
        if len(result[key]) > limit:
            raise TildaInvalid("contact_too_long")
    return result


def digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def product_map():
    """Operator-maintained retail snapshot; never reads a wholesale price."""
    result = json.loads(os.getenv("TILDA_PRODUCT_MAP_JSON", "{}"))
    if not isinstance(result, dict):
        raise TildaInvalid("invalid_product_map")
    for key, value in result.items():
        if (not isinstance(value, dict) or not isinstance(value.get("product_id"), str)
                or not 1 <= len(value["product_id"]) <= 255):
            raise TildaInvalid("invalid_product_mapping")
        if (type(value.get("retail_price_minor")) is not int
                or not 1 <= value["retail_price_minor"] <= 100_000_000_000):
            value["retail_price_minor"] = None
    return result
