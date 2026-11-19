import unittest

from app.services.email_parser import EmailParser


class GmailNumericSkuTest(unittest.TestCase):
    def test_numeric_and_dotted_codes_remain_the_original_articles(self):
        result = EmailParser().parse_message(
            "Заказ", "Добрый день!)\n\n3.4.5a - 1 шт\n009 - 2 шт\n3.1 200 - 3 шт",
        )
        self.assertEqual(result.intent, "order")
        self.assertEqual([(line.raw_product_text, line.qty) for line in result.items], [
            ("3.4.5a", 1), ("009", 2), ("3.1 200", 3),
        ])
        self.assertTrue(all(line.quantity_confidence == "confirmed" for line in result.items))
        self.assertTrue(result.items[1].quantity_evidence.startswith("Артикул 009 |"))

    def test_numeric_sku_in_an_order_subject(self):
        result = EmailParser().parse_message("Заказ: 009 – 2 шт.", "")
        self.assertEqual([(line.raw_product_text, line.qty) for line in result.items], [("009", 2)])

    def test_does_not_invert_quantities_or_convert_questions(self):
        parser = EmailParser()
        for subject, body in [
            ("Сверка", "009 - 2 шт"),
            ("Заказ", "Есть ли 009 - 2 шт?"),
            ("Заказ", "009 - 0 шт\n3.1 200 - 100001 шт"),
            ("Заказ", "009 - 2"),
            ("Заказ", "3 2 шт"),
        ]:
            with self.subTest(subject=subject, body=body):
                self.assertEqual(parser.parse_message(subject, body).items, ())

    def test_preserves_existing_prefix_quantity_for_product_names(self):
        result = EmailParser().parse_message("Заказ", "2 шт Marvis Classic\n3 DSD 500мл")
        self.assertEqual([(line.raw_product_text, line.qty) for line in result.items], [
            ("Marvis Classic", 2), ("DSD 500мл", 3),
        ])
