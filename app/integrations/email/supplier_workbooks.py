"""Read supplier price rows without executing Excel formulas or opening links."""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    currency_evidence: int
    price_columns: tuple[int, ...]


def currencies(text):
    text = re.sub(r"\[\$-[-\w]+\]", "", (text or "").casefold())
    text = re.sub(r"-[0-9a-f]{3,8}(?=\])", "", text)
    values = set()
    if re.search(r"₽|(?<![\w])(?:rub|rur|руб(?:\.|ль|лей|ля)?)(?!\w)|\[\$р\.?(?:\]|-)", text):
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
    if "," in raw and "." in raw:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", raw):
            raw = raw.replace(",", "")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", raw):
            raw = raw.replace(".", "")
        else:
            return None
    if isinstance(value, str) and not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw):
        return None
    try:
        amount = Decimal(str(value)) if not isinstance(value, str) else Decimal(raw.replace(",", "."))
        if not amount.is_finite() or not 0 < amount <= 10**10:
            return None
        minor = int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None
    return minor if minor > 0 else None


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
        title_cells = [value for row in heading[:8] for value in row if value]
        title = " ".join(title_cells)
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
            name_col = name_columns[0]
            sku_col = next(iter(sku_columns)) if len(sku_columns) == 1 else None
            body = rows[header_idx + 1:]
            columns = []
            declared_currency = set().union(*(currencies(cell) for cell in title_cells if TITLE.search(cell)))
            for col in price_columns:
                if col in (name_col, sku_col):
                    continue
                explicit = set().union(*(currencies(h[col]) for h in nearby if col < len(h)))
                inferred = set()
                for row in body:
                    if col < len(row):
                        value, fmt = row[col]
                        if price_minor(value, None) is not None:
                            inferred |= currencies(fmt) | currencies(safe_text(value))
                marks = explicit or inferred or declared_currency
                if len(marks) > 1 or marks - {"RUB", "USD"}:
                    continue
                columns.append((col, next(iter(marks), None), 3 if explicit else 2 if inferred else int(bool(declared_currency))))
            if not columns:
                continue

            # An unlabeled column beside a labeled RUB column follows the
            # purchase mailbox default (USD). Keep the winning cell's currency.
            has_declared_rub = any(currency == "RUB" for _, currency, _ in columns)
            fallback = "USD" if has_declared_rub and len(columns) > 1 else verified_currency
            records, duplicated, conflicts = {}, 0, set()
            winners = Counter()
            for number, row in enumerate(body, header_idx + 2):
                if name_col >= len(row):
                    continue
                raw_name = row[name_col][0]
                name = safe_text(raw_name)
                if not isinstance(raw_name, str) or not name or len(name) > 490:
                    continue
                prices = []
                for col, currency, evidence in columns:
                    if col < len(row):
                        currency = currency or fallback
                        value, _ = row[col]
                        if currency and (amount := price_minor(value, currency)) is not None:
                            prices.append((amount, evidence, col, currency))
                if not prices:
                    continue
                amount, evidence, col, currency = max(prices)
                sku = safe_text(row[sku_col][0]) if sku_col is not None and sku_col < len(row) else ""
                if len(sku) > 255:
                    continue
                key = "sku:" + sku if sku else "name:" + normalize(name)
                if key in records:
                    duplicated += 1
                    previous = records[key]
                    if normalize(previous["name"]) != normalize(name):
                        conflicts.add(key)
                        continue
                    if previous["price_minor"] >= amount:
                        continue
                records[key] = dict(row=number, name=name, sku=sku or None, mapping_key=key,
                                    price_minor=amount, currency_code=currency, price_column=col,
                                    old_price_minor=None, state="new", offer_id=None, error=None, candidates=[])
            for key in conflicts:
                del records[key]
            if len(records) >= 5:
                for record in records.values():
                    winners[(record["price_column"], record["currency_code"])] += 1
                (main_col, main_currency), _ = winners.most_common(1)[0]
                strength = max(evidence for _, _, evidence in columns)
                candidates.append(PriceSheet(sheet, header_idx + 2, name_col, main_col, sku_col, main_currency,
                                             list(records.values()), duplicated + len(conflicts), strength,
                                             tuple(col for col, _, _ in columns)))
    if not candidates:
        return None
    return max(candidates, key=lambda p: (len(p.rows), p.currency_evidence, len(p.price_columns)))
