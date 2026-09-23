"""Read-only FX. All supported currencies currently have two minor digits."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol
from xml.etree import ElementTree
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from app.integrations.http_tls import verified_session

CURRENCIES = frozenset({"RUB", "USD", "EUR", "CNY"})


class FxError(ValueError):
    pass


def rate_value(value) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise FxError("Курс указывается десятичной строкой, без float")
    try:
        rate = Decimal(value)
        if not rate.is_finite() or not Decimal("0") < rate < Decimal("1000000") or rate.as_tuple().exponent < -10:
            raise ValueError
        return rate
    except (InvalidOperation, ValueError) as error:
        raise FxError("Некорректный курс") from error


def convert_minor(amount: int, rate: Decimal) -> int:
    if type(amount) is not int or not 0 <= amount <= 9_223_372_036_854_775_807:
        raise FxError("Стоимость должна быть целым числом minor units")
    result = int((Decimal(amount) * rate_value(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result > 9_223_372_036_854_775_807:
        raise FxError("Стоимость превышает допустимый предел")
    return result


@dataclass(frozen=True)
class FxQuote:
    currency: str
    rate: Decimal
    source: str
    rate_date: date

    def __post_init__(self):
        if self.currency not in CURRENCIES or (self.currency == "RUB" and self.rate != Decimal("1")):
            raise FxError("Некорректная валюта / курс RUB")
        rate_value(self.rate)


class FxRateProvider(Protocol):
    def quote(self, currency: str) -> FxQuote: ...


class FakeFxProvider:
    def __init__(self, rate="90", day=None):
        self.rate, self.day = rate_value(rate), day or date.today()

    def quote(self, currency):
        return FxQuote(currency, Decimal("1") if currency == "RUB" else self.rate, "fake", self.day)


class CbrFxProvider:
    URL = "https://www.cbr.ru/scripts/XML_daily.asp"

    @staticmethod
    def parse(payload: bytes, currency: str) -> FxQuote:
        try:
            if len(payload) > 1_000_000 or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                raise ValueError
            root = ElementTree.fromstring(payload)
            day = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
            for node in root.findall("Valute"):
                if node.findtext("CharCode") == currency:
                    value = Decimal(node.findtext("Value").replace(",", "."))
                    nominal = Decimal(node.findtext("Nominal"))
                    rate = (value / nominal).quantize(Decimal("0.0000000001"))
                    return FxQuote(currency, rate, "cbr", day)
        except (ValueError, KeyError, AttributeError, ArithmeticError, ElementTree.ParseError) as error:
            raise FxError("Некорректный ответ ЦБ") from error
        raise FxError("Курс валюты отсутствует у ЦБ")

    def quote(self, currency):
        if currency == "RUB":
            return FxQuote("RUB", Decimal("1"), "identity", date.today())
        if currency not in CURRENCIES:
            raise FxError("Валюта не поддерживается")
        try:
            with verified_session() as session:
                session.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.3,
                    allowed_methods={"GET"}, status_forcelist=[429, 500, 502, 503, 504], respect_retry_after_header=False)))
                response = session.get(self.URL, timeout=(5, 15))
                response.raise_for_status()
                return self.parse(response.content, currency)
        except requests.RequestException as error:
            raise FxError("ЦБ временно недоступен; повторите или укажите ручной курс") from error
