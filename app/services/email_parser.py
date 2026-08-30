import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedOrderLine:
    raw_product_text: str
    qty: int


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
        extracted = []
        for raw_line in body_text.splitlines():
            line = raw_line.strip().lstrip("•*- ").strip()
            if not line:
                continue
            match = self._quantity_pattern.match(line)
            if match is None:
                match = self._quantity_with_units_pattern.match(line)
            if match is None:
                continue
            product = match.group("product").strip(" -–—")
            qty = int(match.group("qty"))
            if product and qty > 0:
                extracted.append(ExtractedOrderLine(product, qty))
        return extracted
