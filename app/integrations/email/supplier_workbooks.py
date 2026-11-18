"""Read supplier price rows without executing Excel formulas or opening links."""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import PurePath
import csv
import re
from zipfile import ZipFile

from app.services.buying_excel import normalize

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 60_000
MAX_COLUMNS = 64
NAME = re.compile(r"^(наименование|название|номенклатура|описание|товар|name|product|item)(\b|$)")
SKU = re.compile(r"^(артикул|код товара|sku|item code)(\b|$)")
PRICE = re.compile(r"^(?:(?:оптовая|закупочная|розничная|отпускная)\s+)?(?:цена|price)(\b|$)|^(?:wholesale|unit) price\b")
TITLE = re.compile(r"прайс\s*[-–]?\s*лист|price\s*[-–]?\s*list|оптовый прайс", re.I)
OTHER = re.compile(r"(?<!\w)(?:накладная|акт сверки|сч[её]т[- ]фактура|сч[её]т на оплату|invoice|purchase order)(?!\w)", re.I)


@dataclass
class PriceSheet:
    sheet: int
    first_row: int
    name_column: int
    price_column: int
    sku_column: int | None
    currency: str | None
    rows: list
    skipped: int


def currencies(text):
    text = re.sub(r"\[\$-[-\w]+\]", "", (text or "").casefold())
    text = re.sub(r"-[0-9a-f]{3,8}(?=\])", "", text)
    values = set()
    if re.search(r"₽|(?<![\w])(?:rub|rur|руб(?:\.|ль|лей|ля)?)(?!\w)", text):
        values.add("RUB")
    if re.search(r"(?<![\w])(?:usd|долл(?:\.|ар(?:ов|а)?)?)(?!\w)|(?<![\[a-z])\$|\[\$\$", text):
        values.add("USD")
    if re.search(r"€|(?<![\w])(?:eur|евро)(?!\w)", text):
        values.add("EUR")
    return values


def safe_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def price_minor(value, currency):
    if type(value) not in (str, int, float):
        return None
    raw = safe_text(value)
    if currencies(raw) - ({currency} if currency else {"RUB", "USD"}):
        return None
    raw = re.sub(r"(?i)\s*(?:₽|\$|USD|RUB|руб\.?)\s*", "", raw).replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw):
        return None
    try:
        minor = int(Decimal(raw.replace(",", ".")) * 100)
    except (InvalidOperation, ValueError):
        return None
    return minor if 0 < minor <= 10**12 else None


def workbooks(data, filename):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("file_size")
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(entries) > 256 or len(set(names)) != len(names)
                    or sum(entry.file_size for entry in entries) > 65 * 1024 * 1024
                    or any("vbaproject" in name.casefold() or name.startswith(("/", "xl/externalLinks/"))
                           or "\\" in name or ".." in name.split("/") for name in names)):
                raise ValueError("invalid_archive")
        book = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
        try:
            for index, sheet in enumerate(book.worksheets[:12], 1):
                if sheet.sheet_state == "visible":
                    rows = sheet.iter_rows(max_col=min(MAX_COLUMNS, sheet.max_column or MAX_COLUMNS),
                                           max_row=min(MAX_ROWS + 1, sheet.max_row or MAX_ROWS + 1))
                    yield index, ([(c.value, c.number_format or "") for c in row] for row in rows)
        finally:
            book.close()
    elif suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=data, formatting_info=True, on_demand=True,
                                  ragged_rows=True, logfile=StringIO())
        try:
            for index in range(min(book.nsheets, 12)):
                sheet = book.sheet_by_index(index)
                if sheet.visibility != 0 or sheet.nrows > MAX_ROWS:
                    continue
                def rows():
                    for row in range(sheet.nrows):
                        cells = []
                        for col in range(min(MAX_COLUMNS, sheet.row_len(row))):
                            cell = sheet.cell(row, col)
                            fmt = book.format_map.get(book.xf_list[sheet.cell_xf_index(row, col)].format_key)
                            value = cell.value if cell.ctype in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_TEXT) else ""
                            cells.append((value, fmt.format_str if fmt else ""))
                        yield cells
                yield index + 1, rows()
                book.unload_sheet(index)
        finally:
            book.release_resources()
    elif suffix == ".csv":
        source = data.decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(source[:4096], delimiters=",;\t")
        yield 1, ([(cell, "") for cell in row[:MAX_COLUMNS]] for row in csv.reader(StringIO(source), dialect))


def find_price_sheet(data, filename, *, subject="", verified_currency=None):
    if OTHER.search(filename + " " + subject) and not TITLE.search(filename):
        return None
    candidates = []
    for sheet, stream in workbooks(data, filename):
        rows = []
        for row in stream:
            rows.append(row)
            if len(rows) > MAX_ROWS:
                break
        if len(rows) > MAX_ROWS:
            continue
        heading = [[normalize(safe_text(v)) for v, _ in row] for row in rows[:22]]
        title = " ".join(value for row in heading[:8] for value in row if value)
        if OTHER.search(title) and not TITLE.search(title):
            continue
        for header_idx, header in enumerate(heading):
            price_columns = [col for col, v in enumerate(header) if PRICE.search(v) and not TITLE.search(v)]
            if not price_columns:
                continue
            nearby = heading[max(0, header_idx - 4):header_idx + 1]
            name_columns = [col for col, v in enumerate(header) if NAME.search(v)]
            if not name_columns:
                name_columns = sorted({col for h in nearby for col, v in enumerate(h) if NAME.search(v)})
            if not name_columns and TITLE.search(title):
                name_columns = [col for col, v in enumerate(header) if TITLE.search(v)]
            if len(name_columns) != 1:
                continue
            sku_columns = {col for h in nearby for col, v in enumerate(h) if SKU.search(v)}
            for price_col in price_columns:
                name_col = name_columns[0]
                sku_col = next(iter(sku_columns)) if len(sku_columns) == 1 else None
                if price_col == name_col or sku_col in {price_col, name_col}:
                    continue
                explicit = set().union(*(currencies(h[price_col]) for h in nearby if price_col < len(h)))
                inferred = set()
                for row in rows[header_idx + 1:]:
                    if price_col < len(row):
                        value, fmt = row[price_col]
                        if price_minor(value, None) is not None:
                            inferred |= currencies(fmt) | currencies(safe_text(value))
                marks = explicit | inferred
                if not marks:
                    marks = currencies(title)
                    if not marks and verified_currency:
                        marks = {verified_currency}
                currency = next(iter(marks)) if len(marks) == 1 and marks <= {"RUB", "USD"} else None
                records, duplicated, conflicts = {}, 0, set()
                for number, row in enumerate(rows[header_idx + 1:], header_idx + 2):
                    if max(name_col, price_col) >= len(row):
                        continue
                    raw_name = row[name_col][0]
                    name = safe_text(raw_name)
                    price = price_minor(row[price_col][0], currency)
                    if not isinstance(raw_name, str) or not name or price is None or len(name) > 490:
                        continue
                    sku = safe_text(row[sku_col][0]) if sku_col is not None and sku_col < len(row) else ""
                    if len(sku) > 255:
                        continue
                    key = "sku:" + sku if sku else "name:" + normalize(name)
                    if key in records:
                        duplicated += 1
                        if records[key]["price_minor"] != price or normalize(records[key]["name"]) != normalize(name):
                            conflicts.add(key)
                        continue
                    records[key] = dict(row=number, name=name, sku=sku or None, mapping_key=key, price_minor=price,
                                        old_price_minor=None, state="new", offer_id=None, error=None, candidates=[])
                for key in conflicts:
                    del records[key]
                if len(records) >= 5:
                    candidates.append(PriceSheet(sheet, header_idx + 2, name_col, price_col, sku_col, currency,
                                                 list(records.values()), duplicated + len(conflicts)))
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.currency is not None, len(p.rows)))
