"""Offline parser regression for shorthand wholesaler lines with emoji and mobile signatures."""
import ast
from pathlib import Path
from app.services.email_parser import EmailParser


def main():
    ast.parse(Path("app/services/email_parser.py").read_text(encoding="utf-8-sig"))
    parser = EmailParser()
    for body in (
        "Dsd 5.2.1 бальзам -2 шт😻 -- Отправлено из мобильной Яндекс Почты",
        "Dsd 5.2.1 бальзам -2 шт😻",
        "Dsd 5.2.1 бальзам – 2 шт😻\nОтправлено из мобильной Яндекс Почты",
    ):
        parsed = parser.parse_message("Еще на Сашку)", body, sender_name="Жанна Ким")
        assert parsed.intent == "order" and len(parsed.items) == 1, parsed
        assert parsed.items[0].qty == 2, parsed.items
        assert parsed.items[0].raw_product_text == "Dsd 5.2.1 бальзам", parsed.items
    for body in (
        "Есть ли Dsd 5.2.1 бальзам -2 шт😻",
        "Можно ли Dsd 5.2.1 бальзам -2 шт😻?",
        "Сколько стоит Dsd 5.2.1 бальзам -2 шт😻",
    ):
        assert parser.parse_message("Вопрос", body).intent == "inquiry"
    print("UNIFIED_EMAIL_PARSER_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
