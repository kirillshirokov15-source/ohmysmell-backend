"""Safe catalog query language for Buying.

Partial words: `marv mint` matches substrings independently.
Exclusions: `marv 200мл -100мл -150мл` and `-"100 ml"`.
A volume is part of the product name, not an item quantity.
"""
import re
import shlex
from dataclasses import dataclass

from sqlalchemy import and_, or_, func

from app.services.buying_excel import normalize


@dataclass(frozen=True)
class CatalogTerms:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


_UNITS = frozenset({"мл", "ml", "миллилитр", "миллилитра", "миллилитров",
                    "l", "л", "g", "гр", "г", "kg", "кг"})
_VOLUME = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(мл|ml|миллилитр(?:а|ов)?|л|l|гр|г|g|кг|kg)$", re.I)


def parse_catalog_query(raw: str) -> CatalogTerms:
    if not isinstance(raw, str) or len(raw) > 200 or any(ord(ch) < 32 for ch in raw):
        raise ValueError("Некорректная строка поиска")
    try:
        chunks = shlex.split(raw)
    except ValueError as error:
        raise ValueError("Не закрыты кавычки в поиске") from error
    includes, excludes = [], []
    i = 0
    while i < len(chunks):
        raw_token = chunks[i].strip()
        i += 1
        if not raw_token or raw_token == "-":
            continue
        negative = raw_token.startswith("-") and len(raw_token) > 1
        token = raw_token[1:] if negative else raw_token
        # Allow the familiar "200 мл -100 мл" spelling.
        if (token.replace(",", ".").replace(".", "").isdigit()
                and i < len(chunks) and normalize(chunks[i]) in _UNITS):
            token += " " + chunks[i]
            i += 1
        value = normalize(token)
        if not value or value in {"+", "-"}:
            continue
        if len(value) > 100 or not any(ch.isalnum() or ch in "%_" for ch in value):
            raise ValueError("Недопустимый поисковый термин")
        (excludes if negative else includes).append(value)
        if len(includes) + len(excludes) > 16:
            raise ValueError("Слишком много условий поиска")
    return CatalogTerms(tuple(dict.fromkeys(includes)), tuple(dict.fromkeys(excludes)))


def _patterns(term: str) -> tuple[str, ...]:
    match = _VOLUME.fullmatch(term)
    if not match:
        return (term,)
    number, unit = match.groups()
    unit = unit.casefold()
    aliases = ("мл", "ml", "миллилитров", "миллилитра", "миллилитр") if unit.startswith(("мл", "ml", "миллилитр")) else (
        ("л", "l") if unit in {"л", "l"} else
        ("г", "гр", "g") if unit in {"г", "гр", "g"} else ("кг", "kg"))
    return tuple(number + separator + v for v in aliases for separator in ("", " "))


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def buying_filter(terms: CatalogTerms, *columns):
    """Build SQL bind-param-only filters; never interpolate user SQL."""
    def matches(token):
        return or_(*(col.like("%" + _escape(value) + "%", escape="\\")
                     for col in columns for value in _patterns(token)))
    clauses = [matches(term) for term in terms.include]
    clauses.extend(~matches(term) for term in terms.exclude)
    return and_(*clauses) if clauses else None
