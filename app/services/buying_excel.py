"""Bounded, values-only XLSX reader with explicit per-supplier column layouts.

No formulas, macros, external relationships, extraction to disk or type guessing.
"""
from io import BytesIO
from decimal import Decimal, InvalidOperation
from typing import Protocol
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field

MAX_UPLOAD = 2 * 1024 * 1024
MAX_ROWS = 5000
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def normalize(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


class ParserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = "columns-v1"
    sheet: int = Field(default=1, ge=1, le=20)
    first_row: int = Field(default=2, ge=1, le=100)
    name_column: str = Field(default="A", pattern="^[A-Z]{1,2}$")
    price_column: str = Field(default="B", pattern="^[A-Z]{1,2}$")
    sku_column: str | None = Field(default=None, pattern="^[A-Z]{1,2}$")


class PriceListParser(Protocol):
    def parse(self, data: bytes) -> list[dict]: ...


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper() or b"\x00" in data:
        raise ValueError("Unsafe XML")
    return ET.fromstring(data)


class ColumnPriceListParser:
    def __init__(self, config: ParserConfig):
        if config.version != "columns-v1":
            raise ValueError("Unsupported parser version")
        columns = [config.name_column, config.price_column, config.sku_column]
        if len(set(c for c in columns if c)) != len([c for c in columns if c]):
            raise ValueError("Parser columns must differ")
        self.config = config

    def parse(self, data):
        if len(data) > MAX_UPLOAD:
            raise ValueError("Excel upload exceeds 2 MiB")
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 200 or sum(e.file_size for e in entries) > 20 * 1024 * 1024:
                    raise ValueError("Excel expanded size limit exceeded")
                if len({e.filename for e in entries}) != len(entries):
                    raise ValueError("Duplicate ZIP entries")
                for e in entries:
                    if ".." in e.filename.split("/") or e.filename.startswith("/") or "\\" in e.filename:
                        raise ValueError("Unsafe archive path")
                    if e.filename.endswith(".rels"):
                        root = xml(archive.read(e))
                        if any(n.attrib.get("TargetMode") == "External" for n in root):
                            raise ValueError("External Excel relationships are unsupported")
                    if "vbaproject" in e.filename.lower():
                        raise ValueError("Macros are unsupported")
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared = ["".join(n.itertext()) for n in xml(archive.read("xl/sharedStrings.xml"))]
                sheet = xml(archive.read(f"xl/worksheets/sheet{self.config.sheet}.xml"))
                rows = sheet.findall("s:sheetData/s:row", NS)
                if len(rows) > MAX_ROWS + 100:
                    raise ValueError("Excel row limit exceeded")
                result, seen = [], set()
                for row in rows:
                    number = int(row.attrib["r"])
                    if number < self.config.first_row:
                        continue
                    cells, formula = {}, False
                    for cell in row:
                        ref = re.fullmatch(r"([A-Z]+)\d+", cell.attrib.get("r", ""))
                        if not ref:
                            raise ValueError("Invalid cell reference")
                        if cell.find("s:f", NS) is not None:
                            formula = True
                        value = cell.findtext("s:v", "", NS)
                        if cell.attrib.get("t") == "s":
                            value = shared[int(value)]
                        elif cell.attrib.get("t") == "inlineStr":
                            value = "".join(cell.find("s:is", NS).itertext())
                        cells[ref[1]] = value
                    name = str(cells.get(self.config.name_column, "")).strip()
                    raw_price = cells.get(self.config.price_column, "")
                    if not name and raw_price == "":
                        continue
                    entry = {"row": number, "name": name[:500], "sku": None, "error": None}
                    try:
                        if formula or not name or len(name) > 500 or len(normalize(name)) > 495 or any(ord(c) < 32 for c in name):
                            raise ValueError
                        sku = cells.get(self.config.sku_column) if self.config.sku_column else None
                        if sku and (len(sku) > 255 or any(ord(c) < 32 for c in sku)):
                            raise ValueError
                        price = Decimal(str(raw_price).replace(" ", "").replace("\u00a0", "").replace(",", ".")) * 100
                        if not price.is_finite() or price < 0 or price > 10**12 or price != price.to_integral_value():
                            raise ValueError
                        key = "sku:" + sku if sku else "name:" + normalize(name)
                        if key in seen:
                            raise ValueError
                        seen.add(key)
                        entry.update(price_minor=int(price), sku=sku, mapping_key=key)
                    except (ValueError, InvalidOperation):
                        entry["error"] = "Invalid name/price, formula, or duplicate mapping"
                    result.append(entry)
                    if len(result) > MAX_ROWS:
                        raise ValueError("Excel row limit exceeded")
                if not result:
                    raise ValueError("No price rows in configured worksheet")
                return result
        except (BadZipFile, KeyError, IndexError, ET.ParseError, TypeError, AttributeError) as error:
            raise ValueError("Invalid or unsupported XLSX structure") from error
