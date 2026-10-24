"""Read-only, evidence-based MoySklad counterparty resolution.

Never treat recent counterparties or fuzzy names as proof of identity.
Suggested links require explicit manager selection and fresh provider verification.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Protocol

from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway
from app.integrations.moysklad.client import MoySkladClient


class CounterpartySearchProvider(Protocol):
    def search_counterparties(self, query: str) -> list[dict]: ...
    def get_recent_counterparties(self, limit: int = 10) -> list[dict]: ...


def normalize_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ы": "y", "э": "e",
    "ю": "yu", "я": "ya", "ь": "", "ъ": "",
}
_LATIN_TO_CYRILLIC = {
    "shch": "щ", "sch": "щ", "zh": "ж", "kh": "х", "ts": "ц",
    "ch": "ч", "sh": "ш", "yu": "ю", "ya": "я", "yo": "ё",
    **{v: k for k, v in _CYRILLIC_TO_LATIN.items() if len(v) == 1},
}
_LATIN_TO_CYRILLIC.update({"e": "е", "y": "й"})
_LATIN_PATTERN = re.compile("|".join(sorted(_LATIN_TO_CYRILLIC, key=len, reverse=True)))


def transliterate(value):
    """Comparable Latin key for Russian or English spellings, never legal-entity proof."""
    return "".join(_CYRILLIC_TO_LATIN.get(c, c) for c in
                   normalize_text(value).replace("ё", "е"))


def search_aliases(value):
    """Bounded provider lookups for manual candidate discovery."""
    normalized = normalize_text(value).replace("ё", "е")
    if not normalized:
        return ()
    if "@" in normalized or sum(ch.isdigit() for ch in normalized) >= 5:
        return (normalized,)
    aliases = [normalized]
    if re.search("[а-я]", normalized):
        aliases.append(transliterate(normalized))
    elif re.search("[a-z]", normalized):
        aliases.append(_LATIN_PATTERN.sub(
            lambda hit: _LATIN_TO_CYRILLIC[hit.group(0)], normalized))
    compact = "".join(normalized.split())
    if compact != normalized:
        aliases.append(compact)
    return tuple(dict.fromkeys(alias for alias in aliases if len(alias) >= 3))[:3]


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


def emails(value):
    return {v.casefold() for v in re.findall(
        r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}", str(value or ""), re.I)}


class CounterpartyMatchingService:
    def __init__(self, provider: CounterpartySearchProvider | None = None) -> None:
        self.provider = provider or MoySkladClient()
        self.async_gateway = AsyncMoySkladGateway(self.provider)

    @staticmethod
    def evidence(item, *, email=None, name=None, phone=None, inn=None):
        if item.get("archived"):
            return None
        if inn and str(item.get("inn") or "").strip() == str(inn).strip():
            return "ИНН – точное совпадение"
        if email and email.casefold() in emails(item.get("email")):
            return "Email – точное совпадение"
        if phone and normalize_phone(phone) and normalize_phone(phone) == normalize_phone(item.get("phone")):
            return "Телефон – точное совпадение"
        if name and normalize_text(name) == normalize_text(item.get("name")):
            return "Название – точное совпадение (требует сверки)"
        return None

    @classmethod
    def _candidate(cls, item, *, reason=None):
        return {
            "id": item["id"],
            "name": item.get("name"),
            "email": item.get("email"),
            "phone": item.get("phone"),
            "inn": item.get("inn"),
            "reason": reason or "Похожее имя – проверьте данные",
        }

    @staticmethod
    def _queries(*queries):
        seen = set()
        for raw in queries:
            val = str(raw or "").strip()[:320]
            normalized = normalize_text(val)
            if len(val) >= 3 and normalized not in seen:
                seen.add(normalized)
                yield val

    @classmethod
    def _rank(cls, results, *, email=None, name=None, phone=None, inn=None):
        unique = {r["id"]: r for r in results if r.get("id") and not r.get("archived")}
        scored = []
        for item in unique.values():
            evidence = cls.evidence(item, email=email, name=name, phone=phone, inn=inn)
            if not evidence:
                continue
            rank = 0 if evidence.startswith("ИНН") else 1 if evidence.startswith("Email") else (
                2 if evidence.startswith("Телефон") else 3)
            scored.append((rank, normalize_text(item.get("name")), cls._candidate(item, reason=evidence)))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [x[2] for x in scored[:10]]

    async def candidates_async(self, *queries: str | None) -> list[dict]:
        queries = list(self._queries(*queries))
        if not queries:
            return []
        results = []
        # Search all identity dimensions, never stop at the first name hit.
        for query in queries[:5]:
            results.extend(await self.async_gateway.search_counterparties(query))
            if hasattr(self.provider, "find_counterparties_exact"):
                field = "email" if "@" in query else "name"
                results.extend(await self.async_gateway.find_counterparties_exact(field, query))
        email = next((q for q in queries if "@" in q), None)
        phone = next((q for q in queries if normalize_phone(q) and len(normalize_phone(q)) >= 10), None)
        name = next((q for q in queries if "@" not in q and q != phone), None)
        return self._rank(results, email=email, name=name, phone=phone)

    def candidates(self, *queries: str | None) -> list[dict]:
        queries = list(self._queries(*queries))
        results = []
        for query in queries[:5]:
            results.extend(self.provider.search_counterparties(query))
            if hasattr(self.provider, "find_counterparties_exact"):
                field = "email" if "@" in query else "name"
                results.extend(self.provider.find_counterparties_exact(field, query))
        email = next((q for q in queries if "@" in q), None)
        phone = next((q for q in queries if normalize_phone(q) and len(normalize_phone(q)) >= 10), None)
        name = next((q for q in queries if "@" not in q and q != phone), None)
        return self._rank(results, email=email, name=name, phone=phone)

    async def fallback_candidates_async(self, *queries: str | None):
        # Recent unrelated counterparties must NEVER become suggestions.
        return await self.candidates_async(*queries)

    def fallback_candidates(self, *queries: str | None):
        return self.candidates(*queries)

    async def verify_counterparty(self, identifier, *, email=None, name=None, phone=None):
        record = await self.async_gateway.get_counterparty(identifier)
        if str(record.get("id")) != str(identifier) or record.get("archived"):
            raise ValueError("Контрагент удалён, архивирован или ID изменён")
        evidence = self.evidence(record, email=email, name=name, phone=phone)
        if evidence is None:
            raise ValueError("Контрагент не соответствует отправителю; проверьте реквизиты вручную")
        return self._candidate(record, reason=evidence)

    async def manual_search_async(self, query):
        """Return explicit manager choices, including transliteration; never auto-link."""
        value = " ".join(str(query or "").strip().split())
        if len(value) < 3 or len(value) > 150:
            raise ValueError("Укажите название или реквизит (от 3 до 150 символов)")
        matches = []
        for alias in search_aliases(value):
            matches.extend(await self.async_gateway.search_counterparties(alias))
        if value.isdigit() and len(value) in {10, 12} and hasattr(self.provider, "find_counterparties_exact"):
            matches.extend(await self.async_gateway.find_counterparties_exact("inn", value))
        if "@" in value and hasattr(self.provider, "find_counterparties_exact"):
            matches.extend(await self.async_gateway.find_counterparties_exact("email", value))
        unique = {row["id"]: row for row in matches if row.get("id") and not row.get("archived")}
        wanted = transliterate(value).replace(" ", "")
        def rank(item):
            candidate = transliterate(item.get("name")).replace(" ", "")
            exact = wanted == candidate
            contained = bool(wanted and wanted in candidate)
            score = SequenceMatcher(None, wanted, candidate).ratio() if wanted and candidate else 0
            return (-int(exact), -int(contained), -score, normalize_text(item.get("name")))
        sorted_rows = sorted(unique.values(), key=rank)
        # The provider has already restricted the candidate set. A weak fuzzy
        # result is still a suggestion, NOT verified legal-entity identity.
        return [self._candidate(row, reason="Поиск менеджера – сверить реквизиты")
                for row in sorted_rows[:5]]

    async def duplicate_candidates(self, *, name, email=None, phone=None, inn=None):
        """Fail closed if even one likely existing record is found."""
        candidates = []
        for field, value in (("email", email), ("phone", phone), ("inn", inn), ("name", name)):
            if value:
                candidates.extend(await self.async_gateway.find_counterparties_exact(field, str(value)))
        for query in self._queries(email, phone, inn, name):
            for alias in search_aliases(query):
                candidates.extend(await self.async_gateway.search_counterparties(alias))
        # Name and email weak matches should also block create for review.
        seen = set()
        output = []
        for item in candidates:
            i = item.get("id")
            if not i or i in seen:
                continue
            seen.add(i)
            reason = ("Архивная карточка – восстановить, не дублировать" if item.get("archived")
                      else self.evidence(item, email=email, name=name, phone=phone, inn=inn))
            output.append(self._candidate(item, reason=reason))
        return output[:20]
