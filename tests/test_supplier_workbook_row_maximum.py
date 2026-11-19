from io import BytesIO
import unittest

from openpyxl import Workbook

from app.integrations.email.supplier_workbooks import find_price_sheet


class SupplierWorkbookRowMaximumTest(unittest.TestCase):
    def test_selects_each_rows_highest_price_and_its_currency(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["Прайс-лист"])
        sheet.append(["Артикул", "Наименование", "Цена", "Цена руб.", "Количество", "Сумма"])
        rows = [(30, 2550), (10, "850,00 руб."), (12, 1020.000000000001),
                (15, None), (20, 19), (12, 12)]
        for number, (usd, rub) in enumerate(rows):
            sheet.append([f"00{number}", f"Товар {number}", usd, rub, 100000, 123456])
        stream = BytesIO()
        book.save(stream)
        result = find_price_sheet(stream.getvalue(), "price.xlsx", verified_currency="USD")
        self.assertEqual(result.price_columns, (2, 3))
        self.assertEqual([(row["price_minor"], row["currency_code"]) for row in result.rows],
                         [(255000, "RUB"), (85000, "RUB"), (102000, "RUB"),
                          (1500, "USD"), (2000, "USD"), (1200, "RUB")])
        self.assertEqual(result.rows[0]["sku"], "000")
