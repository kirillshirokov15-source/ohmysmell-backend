"""Conservative parser for customer Gmail wholesale orders.

Explicit quantity is accepted in the email subject OR body. Free prose,
greetings and signatures never silently become order positions. Unclear item
quantities are still visible for manager review when the customer explicitly
indicates an order. Questions are not turned into purchase orders.
"""
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedOrderLine:
    raw_product_text: str
    qty: int
    quantity_confidence: str = "confirmed"
    quantity_evidence: str = ""


@dataclass(frozen=True)
class ClassifiedEmail:
    intent: str   # "order" or "inquiry"
    items: tuple[ExtractedOrderLine, ...]


class EmailParser:
    _suffix_qty = re.compile(
        r"^(?P<product>.+?)\s*(?:(?:[-–—]\s*)|(?:[xх×]\s*))"
        r"(?P<qty>\d+)\s*(?:шт\.?|штук(?:и|а)?|pcs?\.?)?\s*$", re.I,
    )
    _plain_qty = re.compile(
        r"^(?P<product>.+?)\s+(?P<qty>\d+)\s*"
        r"(?:шт\.?|штук(?:и|а)?|pcs?\.?)\s*$", re.I,
    )
    _prefix_qty = re.compile(
        r"^(?P<qty>\d+)\s*(?:шт\.?|штук(?:и|а)?|pcs?\.?|[xх×])?\s+"
        r"(?P<product>[^\n]+?)\s*$", re.I,
    )
    _inline_qty = re.compile(r"^(?P<product>.+?)\s+qty\s*[:=]?\s*(?P<qty>\d+)\s*$", re.I)
    _list_marker = re.compile(r"^\s*(?:[•*]|\d+[.)])\s+", re.I)
    _order_header = re.compile(r"^\s*(?:заказ(?:ы|ать)?|заказыва[ею]м|"
        r"хочу заказать|прошу заказать|order|items?|список товаров)\s*:?\s*$", re.I)
    _order_signal = re.compile(r"\b(?:заказ(?:ы|ать|ать)?|заказыва\w*|"
        r"оформите\s+заказ|order|purchase\s+order|please\s+send)\b", re.I)
    _question_signal = re.compile(
        r"(?:\bуточн\w*|\bналичи\w*|\bинтересует\b|"
        r"\bподскаж\w*|\bвопрос\b|\bскажите\b|"
        r"\bесть\s+ли\b|\bможно\s+ли\b|\bсколько\s+сто\w*|"
        r"\bцена\b|\bцены\b|\bпрайс\b|"
        r"\bavailability\b|\bquestion\b|\binquiry\b|"
        r"\bhow\s+much\b|\bprice\b|\bquotation\b)", re.I,
    )
    _explicit_order = re.compile(
        r"(?:\bзаказыва\w*|\bоформите\s+заказ\b|"
        r"\bпришлите\s+товары\b|\bпрошу\s+отгрузить\b|"
        r"\bplease\s+(?:ship|place\s+my\s+order)\b)", re.I,
    )
    _signature_start = re.compile(
        r"^\s*(?:с\s+уважением\b|best\s+regards\b|kind\s+regards\b|"
        r"regards\b|sent\s+from\b|отправлено\s+с\b|"
        r"--\s*$|_{3,}|={3,})", re.I,
    )
    _greeting = re.compile(
        r"^(?:здравствуйте|здравствуй(?:те)?|добрый\s+(?:день|вечер|утро)|"
        r"спасибо|благодарю|hello|hi|dear|thanks|итого|всего|total|subtotal)\b", re.I,
    )
    _header_names = frozenset({"product", "name", "наименование", "товар"})
    _header_quantities = frozenset({"qty", "quantity", "количество", "кол-во", "шт", "шт."})
    _product_prefix = re.compile(r"^\s*(?:заказ\s*[:№-]?\s*|order\s*[:#-]?\s*)", re.I)
    _inline_order_name = re.compile(r"^\s*(?:заказ|order)\s*[:\-]\s*(?P<product>.+)$", re.I)

    @staticmethod
    def _normalize(value):
        return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())

    @classmethod
    def _safe_product(cls, name, *, sender_name=None):
        name = name.strip(" \t:;,.–—-")
        if not name or len(name) > 450 or "@" in name or not re.search(r"[a-zа-яё]", name, re.I):
            return None
        if cls._greeting.match(name) or cls._signature_start.match(name):
            return None
        if cls._normalize(name) == cls._normalize(sender_name):
            return None
        if name.endswith("?") or name.startswith(("http://", "https://")):
            return None
        return name

    @classmethod
    def _line_qty(cls, line):
        for pattern in (cls._suffix_qty, cls._plain_qty, cls._inline_qty, cls._prefix_qty):
            m = pattern.match(line)
            if m:
                product = cls._safe_product(m.group("product"))
                qty = int(m.group("qty"))
                if product and 1 <= qty <= 100000:
                    return ExtractedOrderLine(product, qty, "confirmed", line[:500])
        return None

    @classmethod
    def _subject_items(cls, subject):
        subject = cls._product_prefix.sub("", subject or "").strip()
        # Split only conventional delimiters. Preserve product names with commas
        # unless every candidate looks like a separately quantified line.
        parts = [p.strip() for p in re.split(r"\s*[;,\n]\s*", subject) if p.strip()]
        if len(parts) == 1:
            parts = [p.strip() for p in re.split(r"\s+\+\s+|\s+и\s+(?=\S+\s+(?:[xх×]|\d))", parts[0]) if p.strip()]
        results = []
        for p in parts:
            row = cls._line_qty(p)
            if row:
                results.append(row)
        if not results:
            candidate = cls._inline_order_name.match(subject or "")
            if candidate and (name := cls._safe_product(candidate.group("product"))):
                results.append(ExtractedOrderLine(name, 0, "unknown", (subject or "")[:500]))
        return results

    def parse_lines(self, body_text: str, *, sender_name=None,
                    explicit_order=False) -> list[ExtractedOrderLine]:
        if "<table" in body_text.lower():
            from app.integrations.email.gmail_provider import _HTMLTextExtractor
            extractor = _HTMLTextExtractor()
            extractor.feed(body_text)
            body_text = extractor.text()
        output = []
        lines = body_text.splitlines()
        skip = set()
        product_column, qty_column, in_order_list = 0, None, False
        for index, raw in enumerate(lines):
            if index in skip:
                continue
            line = raw.strip()
            if self._signature_start.match(line):
                break
            if not line:
                in_order_list = False
                continue
            if self._normalize(line) == self._normalize(sender_name):
                continue
            if self._order_header.match(line):
                in_order_list = True
                continue
            inline_name = self._inline_order_name.match(line)
            if explicit_order and inline_name:
                name = self._safe_product(inline_name.group("product"), sender_name=sender_name)
                if name:
                    output.append(ExtractedOrderLine(name, 0, "unknown", line[:500]))
                    continue
            marker = self._list_marker.match(line)
            clean = line[marker.end():].strip() if marker else line.lstrip("*•- ").strip()
            if self._greeting.match(clean) or "@" in clean or len(clean) > 500:
                continue
            cells = [part.strip() for part in re.split(r"\t|\|", clean)]
            if len(cells) > 1:
                titles = [self._normalize(s) for s in cells]
                if (any(t in self._header_names for t in titles)
                        and any(t in self._header_quantities for t in titles)):
                    product_column = next((i for i,t in enumerate(titles) if t in self._header_names), 0)
                    qty_column = next((i for i,t in enumerate(titles) if t in self._header_quantities), None)
                    in_order_list = True
                    continue
                product = cells[product_column] if product_column < len(cells) else ""
                quant = cells[qty_column] if qty_column is not None and qty_column < len(cells) else (
                    cells[1] if len(cells) == 2 else "")
                match = re.fullmatch(r"(?:qty\s*)?(\d+)\s*(?:шт\.?|pcs?\.?)?", quant, re.I)
                if product and match and (name := self._safe_product(product, sender_name=sender_name)):
                    qty = int(match.group(1))
                    if 1 <= qty <= 100000:
                        output.append(ExtractedOrderLine(name, qty,
                            "confirmed" if qty_column is not None else "probable", clean[:500]))
                        continue
            parsed = self._line_qty(clean)
            if parsed:
                # Do not parse a question such as "Есть ли Marvis – 2 шт?"
                if not self._question_signal.search(clean) and "?" not in clean:
                    if self._normalize(parsed.raw_product_text) != self._normalize(sender_name):
                        output.append(parsed)
                continue
            if not explicit_order:
                continue
            # Unquantified product lines are candidates ONLY under an explicit
            # order section, not an arbitrary paragraph or signature.
            if not (in_order_list or marker):
                continue
            name = self._safe_product(clean, sender_name=sender_name)
            if not name:
                continue
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            adjacent = re.fullmatch(r"(?:qty\s*)?(\d+)\s*(?:шт\.?|pcs?\.?)?", next_line, re.I)
            if adjacent and 1 <= int(adjacent[1]) <= 100000:
                skip.add(index + 1)
                output.append(ExtractedOrderLine(name, int(adjacent[1]), "probable",
                                                (clean + "\n" + next_line)[:500]))
            else:
                output.append(ExtractedOrderLine(name, 0, "unknown", clean[:500]))
        return output

    def parse_message(self, subject, body_text, *, sender_name=None):
        subj = subject or ""
        body = body_text or ""
        order_command = bool(self._explicit_order.search(subj + "\n" + body))
        question = bool(self._question_signal.search(subj + "\n" + body))
        # An inquiry wins unless there is an explicit commitment to buy.
        if question and not order_command:
            return ClassifiedEmail("inquiry", ())
        item_rows = self.parse_lines(body, sender_name=sender_name,
                                     explicit_order=bool(self._order_signal.search(subj + "\n" + body)))
        if subj and not question:
            subject_rows = self._subject_items(subj)
            seen = {self._normalize(i.raw_product_text) for i in item_rows}
            item_rows.extend(i for i in subject_rows
                             if self._normalize(i.raw_product_text) not in seen)
        if not item_rows and not order_command and not self._order_signal.search(subj):
            return ClassifiedEmail("inquiry", ())
        return ClassifiedEmail("order", tuple(item_rows))
