"""Pure parser smoke for three-line SKU/name/quantity wholesale emails."""
import ast
import os
from pathlib import Path
from app.services.email_parser import EmailParser


def main():
    ast.parse(Path("app/services/email_parser.py").read_text(encoding="utf-8-sig"))
    parser = EmailParser()
    body = (
        "RHD028\n\n"
        "Rhode Highlighting Milk 01 » Pearly pink «— Молочко- Хайлайтер\n\n"
        "1\n\n"
        "Отправлено из мобильной Почты Mail"
    )
    os.environ["OMS_GMAIL_MULTILINE_SKU_ENABLED"] = "true"
    classified = parser.parse_message("Заказ", body, sender_name="Андрей Мирзоян")
    assert classified.intent == "order"
    assert len(classified.items) == 1, classified.items
    entry = classified.items[0]
    assert entry.qty == 1 and entry.quantity_confidence == "confirmed"
    assert entry.raw_product_text.startswith("Rhode Highlighting Milk")
    assert entry.quantity_evidence.startswith("Артикул RHD028 |")
    # Safe negatives: a question must not become an order and quantities
    # in a signature or random text cannot be treated as purchased units.
    assert not parser.parse_message("Вопрос по заказу", "Есть ли RHD028?\n\n1").items
    assert not parser.parse_message("Привет", body).items
    os.environ["OMS_GMAIL_MULTILINE_SKU_ENABLED"] = "false"
    assert not parser.parse_message("Заказ", body).items
    print("GMAIL_MULTILINE_SKU_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
