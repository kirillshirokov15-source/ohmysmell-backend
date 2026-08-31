"""Manager-facing Telegram display mappings and formatters."""

from app.config.settings import settings


CUSTOMER_TYPE_LABELS = {
    "unknown": "Не определён",
    "wholesale": "Оптовый",
    "retail": "Розничный",
}
DRAFT_STATUS_LABELS = {
    "draft": "черновик",
    "needs_review": "требует проверки",
    "ready": "готов к подтверждению",
    "new": "новый заказ",
    "created_in_moysklad": "создан в МойСклад",
    "rejected": "отклонён",
}
PRODUCT_MATCH_LABELS = {
    "matched": "найден",
    "ambiguous": "нужно выбрать",
    "not_found": "не найден",
}


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def customer_type_label(value) -> str:
    return CUSTOMER_TYPE_LABELS.get(_value(value), "Не определён")


def draft_status_label(value) -> str:
    return DRAFT_STATUS_LABELS.get(_value(value), "неизвестен")


def product_match_label(value) -> str:
    return PRODUCT_MATCH_LABELS.get(_value(value), "неизвестен")


def telegram_rubles(amount_minor: int) -> str:
    rubles, kopecks = divmod(amount_minor, 100)
    rubles_text = f"{rubles:,}".replace(",", " ")
    return (
        f"{rubles_text},{kopecks:02d} ₽"
        if kopecks
        else f"{rubles_text} ₽"
    )


def review_problem_lines(draft) -> list[str]:
    problems = []
    customer_type = _value(draft.customer_type)
    if customer_type == "unknown":
        problems.append("Необходимо определить тип клиента")
    if not draft.counterparty_id:
        problems.append("Необходимо выбрать контрагента")

    retail_price_missing = False
    for item in draft.items:
        match_status = _value(item.match_status)
        if match_status == "ambiguous":
            problems.append(
                f"Необходимо выбрать товар: {item.raw_product_text}"
            )
        elif match_status == "not_found":
            problems.append(f"Товар не найден: {item.raw_product_text}")
        elif item.price is None and customer_type != "unknown":
            if (
                customer_type == "retail"
                and not settings.moysklad_retail_price_type
            ):
                retail_price_missing = True
            else:
                problems.append(
                    f"Не удалось определить цену: {item.raw_product_text}"
                )
    if retail_price_missing:
        problems.append("Розничная цена не настроена")
    return problems
