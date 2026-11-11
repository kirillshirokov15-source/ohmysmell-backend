"""No-network regression for SKU-only orders without invented quantity."""
import ast
from pathlib import Path
from app.services.email_parser import EmailParser


def main():
    ast.parse(Path("app/services/email_parser.py").read_text(encoding="utf-8-sig"))
    parser = EmailParser()
    parsed = parser.parse_message("Заказ", "KMU024", sender_name="Николай")
    assert parsed.intent == "order" and len(parsed.items) == 1, parsed
    item = parsed.items[0]
    assert item.raw_product_text == "KMU024" and item.qty == 0, item
    assert item.quantity_confidence == "unknown"
    candidate = parser.parse_message("KMU024", "KMU024")
    assert candidate.intent == "possible_order" and candidate.items[0].qty == 0
    assert parser.parse_message("Есть ли KMU024?", "KMU024").intent == "inquiry"
    assert parser.parse_message("Заказ", "Hello").items == ()
    assert parser.parse_message("Заказ", "km u024").items == ()
    assert parser.parse_message("Заказ", "KMU024\nanything else").items == ()
    print("GMAIL_SKU_ONLY_378_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
