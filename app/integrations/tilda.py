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


class TildaConfigurationError(ValueError):
    """Only a safe category is exposed, never the malformed variable value."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TildaInvalid("duplicate_field")
        result[key] = value
    return result


def decode_json(value):
    def invalid_constant(_):
        raise TildaInvalid("invalid_json_number")
    return json.loads(value, parse_float=Decimal, object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def field_map(variable, defaults):
    try:
        mapping = json.loads(os.getenv(variable, "{}"))
        if not isinstance(mapping, dict) or not mapping.keys() <= defaults.keys():
            raise ValueError()
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,4}", v)
               for v in mapping.values()):
            raise ValueError()
        return {**defaults, **mapping}
    except (ValueError, TypeError):
        raise TildaConfigurationError("invalid_tilda_field_map") from None


def form_object(pairs):
    """Bounded bracket encoding, including payment[products][0][externalid].

    Never merge a scalar with an object, or ambiguous/non-contiguous indexes.
    This is a transport adapter; the site's actual paths still require fixtures.
    """
    result = {}
    for key, value in pairs:
        if "[" not in key and "]" not in key:
            parts = [key]
        else:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\[(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]?)\]){1,5}", key):
                raise TildaInvalid("invalid_array")
            parts = re.findall(r"[^\[\]]+", key)
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise TildaInvalid("ambiguous_field")
        if parts[-1] in node:
            raise TildaInvalid("duplicate_field")
        node[parts[-1]] = value

    def arrays(node):
        if not isinstance(node, dict):
            return node
        numeric = [key.isascii() and key.isdigit() for key in node]
        if any(numeric):
            if not all(numeric) or sorted(map(int, node)) != list(range(len(node))):
                raise TildaInvalid("invalid_array")
            return [arrays(node[str(i)]) for i in range(len(node))]
        return {key: arrays(value) for key, value in node.items()}
    return arrays(result)


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
            value = decode_json(body)
        elif content_type == "application/x-www-form-urlencoded":
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=1500, errors="strict")
            value = form_object(pairs)
        else:
            raise TildaInvalid("unsupported_content_type")
        if not isinstance(value, dict):
            raise TildaInvalid("invalid_object")
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise TildaInvalid("invalid_payload") from None


def path_value(payload, path):
    value = payload
    for part in path.split("."):
        if isinstance(value, str):
            try:
                value = decode_json(value)
            except (ValueError, RecursionError):
                return None
        value = value.get(part) if isinstance(value, dict) else None
    return value


def normalize(payload, delivery_key=None):
    fields = field_map("TILDA_FIELD_MAP_JSON", DEFAULT_FIELDS)
    get = lambda key: path_value(payload, fields[key])
    external = get("external_id")
    if external in (None, ""):
        external = delivery_key
    if type(external) not in (str, int) or not 1 <= len(str(external).strip()) <= 200:
        raise TildaInvalid("stable_external_id_required")
    items = get("items")
    if isinstance(items, str):
        try:
            items = decode_json(items)
        except (ValueError, RecursionError):
            raise TildaInvalid("invalid_cart") from None
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise TildaInvalid("invalid_cart")
    item_fields = field_map("TILDA_ITEM_FIELD_MAP_JSON", {"id": "externalid", "name": "name", "qty": "quantity", "price": "price"})
    problems, rows = [], []
    for item in items:
        if not isinstance(item, dict):
            raise TildaInvalid("invalid_item")
        val = lambda key: path_value(item, item_fields[key])
        try:
            raw_qty = val("qty")
            if isinstance(raw_qty, (float, bool)):
                raise TildaInvalid("invalid_quantity")
            qty = Decimal(str(raw_qty))
            if not qty.is_finite() or not 1 <= qty <= 10000 or qty != qty.to_integral_value():
                raise TildaInvalid("invalid_quantity")
        except InvalidOperation:
            raise TildaInvalid("invalid_quantity") from None
        identifier, name = val("id"), val("name")
        if identifier is not None and type(identifier) not in (str, int):
            raise TildaInvalid("invalid_product_id")
        if name is not None and not isinstance(name, str):
            raise TildaInvalid("invalid_item_name")
        identifier, name = str(identifier or ""), name or "Товар"
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
    try:
        result = json.loads(os.getenv("TILDA_PRODUCT_MAP_JSON", "{}"))
    except ValueError:
        raise TildaConfigurationError("invalid_product_map") from None
    if not isinstance(result, dict):
        raise TildaConfigurationError("invalid_product_map")
    for key, value in result.items():
        if (not isinstance(value, dict) or not isinstance(value.get("product_id"), str)
                or not 1 <= len(value["product_id"]) <= 255):
            raise TildaConfigurationError("invalid_product_mapping")
        if (type(value.get("retail_price_minor")) is not int
                or not 1 <= value["retail_price_minor"] <= 100_000_000_000):
            value["retail_price_minor"] = None
    return result
