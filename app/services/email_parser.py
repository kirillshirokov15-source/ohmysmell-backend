import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedOrderLine:
    raw_product_text: str
    qty: int
    quantity_confidence: str = "confirmed"
    quantity_evidence: str = ""


class EmailParser:
    _quantity_pattern = re.compile(
        r"^(?P<product>.+?)\s*(?:(?:[-–—]\s*)|(?:[xх×]\s*))"
        r"(?P<qty>\d+)\s*(?:шт\.?|pcs\.?)?\s*$",
        re.IGNORECASE,
    )
    _quantity_with_units_pattern = re.compile(
        r"^(?P<product>.+?)\s+(?P<qty>\d+)\s*(?:шт\.?|pcs\.?)\s*$",
        re.IGNORECASE,
    )

    def parse_lines(self, body_text: str) -> list[ExtractedOrderLine]:
        if "<table" in body_text.lower():
            from app.integrations.email.gmail_provider import _HTMLTextExtractor
            parser = _HTMLTextExtractor()
            parser.feed(body_text)
            body_text = parser.text()
        extracted = []
        lines = body_text.splitlines()
        skip = set()
        qty_column = None
        product_column = 0
        for index, raw_line in enumerate(lines):
            if index in skip:
                continue
            line = raw_line.strip().lstrip("•*- ").strip()
            if not line:
                continue
            if re.match(r"^(итого|всего|total|количество|qty|quantity|наименование|товар|product|name)\b", line, re.I) and ("\t" in line or "|" in line):
                headers = re.split(r"\t|\|", line)
                qty_column = next((i for i,h in enumerate(headers) if re.fullmatch(r"\s*(qty|quantity|количество|кол-во|шт\.?)\s*",h,re.I)),None)
                product_column = next((i for i,h in enumerate(headers) if re.fullmatch(r"\s*(product|name|наименование|товар)\s*",h,re.I)),0)
                continue
            if re.match(r"^(итого|всего|total|subtotal|здравствуйте|добрый\s|спасибо|с уважением|hello\b|hi\b|thanks\b|наименование\b|количество\b)", line, re.I):
                continue
            cells = [s.strip() for s in re.split(r"\t|\|", line)]
            if len(cells) > 1:
                name = cells[product_column] if product_column < len(cells) else ""
                value = cells[qty_column] if qty_column is not None and qty_column < len(cells) else (cells[1] if len(cells)==2 else "")
                match_qty = re.fullmatch(r"(?:qty\s*)?(\d+)\s*(?:шт\.?|pcs\.?)?",value,re.I)
                if name and re.search(r"[a-zа-я]",name,re.I):
                    qty = int(match_qty[1]) if match_qty and 0 < int(match_qty[1]) <= 100000 else 0
                    extracted.append(ExtractedOrderLine(name,qty,"confirmed" if qty and qty_column is not None else "probable" if qty else "unknown",line[:500]))
                    continue
            match = self._quantity_pattern.match(line)
            if match is None:
                match = self._quantity_with_units_pattern.match(line)
            if match is None:
                match = re.match(r"^(?P<product>.+?)\s+qty\s*[:=]?\s*(?P<qty>\d+)\s*$",line,re.I)
            if match is None:
                if not re.search(r"[a-zа-я]",line,re.I) or len(line)>500 or line.endswith(":") or "@" in line:
                    continue
                next_line = lines[index+1].strip() if index+1<len(lines) else ""
                adjacent = re.fullmatch(r"(?:qty\s*)?(\d+)\s*(?:шт\.?|pcs\.?)?",next_line,re.I)
                qty = int(adjacent[1]) if adjacent and 0<int(adjacent[1])<=100000 else 0
                if adjacent:
                    skip.add(index+1)
                extracted.append(ExtractedOrderLine(line,qty,"probable" if qty else "unknown",(line+"\n"+next_line)[:500]))
                continue
            product = match.group("product").strip(" -–—")
            qty = int(match.group("qty"))
            if product and 0 < qty <= 100000:
                extracted.append(ExtractedOrderLine(product, qty, "confirmed",line[:500]))
            elif product:
                extracted.append(ExtractedOrderLine(product,0,"unknown",line[:500]))
        return extracted
