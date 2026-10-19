from typing import Protocol
import re
import unicodedata
from difflib import SequenceMatcher

from app.integrations.moysklad.async_gateway import AsyncMoySkladGateway
from app.integrations.moysklad.client import MoySkladClient


class CounterpartySearchProvider(Protocol):
    def search_counterparties(self, query: str) -> list[dict]: ...
    def get_recent_counterparties(self, limit: int = 10) -> list[dict]: ...


class CounterpartyMatchingService:
    def __init__(self, provider: CounterpartySearchProvider | None = None) -> None:
        self.provider = provider or MoySkladClient()
        self.async_gateway = AsyncMoySkladGateway(self.provider)

    @staticmethod
    def _candidate(item: dict) -> dict:
        return {
            "id": item["id"],
            "name": item.get("name"),
            "email": item.get("email"),
            "phone": item.get("phone"),
            "inn": item.get("inn"),
            "reason": "недавняя карточка; подтвердите реквизиты",
        }

    @staticmethod
    def normalize(value):
        return " ".join(unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е").split())

    _alphabet = dict(zip("абвгдежзийклмнопрстуфхцчшщыэюя",
        ("a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "y", "e", "yu", "ya")))

    @classmethod
    def transliterate(cls, value):
        return "".join(cls._alphabet.get(c, c) for c in cls.normalize(value) if c not in "ьъ")

    @classmethod
    def variants(cls, query):
        value = cls.normalize(query)
        if "@" in value or any(c.isdigit() for c in value):
            return [value]
        reverse = {v: k for k, v in cls._alphabet.items()}
        reverse.update({"e": "е", "y": "й"})
        pattern = "|".join(sorted(reverse, key=len, reverse=True))
        cyrillic = re.sub(pattern, lambda m: reverse[m[0]], value)
        return list(dict.fromkeys((value, cls.transliterate(value), cyrillic)))

    @classmethod
    def ranked(cls, items, queries):
        ranked = []
        for item in items.values():
            if not item.get("id") or item.get("archived"):
                continue
            score, reason = 0, ""
            for query in queries:
                q = cls.normalize(query)
                if not q:
                    continue
                digits = re.sub(r"\D", "", q)
                for field, label in (("inn", "ИНН"), ("email", "email"), ("phone", "телефон")):
                    value = cls.normalize(item.get(field))
                    matched = q == value if field != "phone" else bool(len(digits) >= 7 and digits == re.sub(r"\D", "", value))
                    if value and matched:
                        score, reason = 2, "точное совпадение: " + label
                similarity = SequenceMatcher(None, cls.transliterate(q), cls.transliterate(item.get("name"))).ratio()
                if similarity >= .6 and similarity > score:
                    score, reason = similarity, "название / транслитерация; подтвердите реквизиты"
            if score:
                ranked.append((score, str(item["id"]), {**cls._candidate(item), "inn": item.get("inn"), "reason": reason}))
        return [entry[2] for entry in sorted(ranked, key=lambda x: (-x[0], x[1]))[:5]]

    def candidates(self, *queries: str | None) -> list[dict]:
        items = {}
        ordered = sorted((q for q in queries if q and q.strip()), key=lambda q: not ("@" in q or any(c.isdigit() for c in q)))
        for query in dict.fromkeys(v for q in ordered for v in self.variants(q)):
            for item in self.provider.search_counterparties(query):
                if item.get("id"):
                    items[item["id"]] = item
        return self.ranked(items, queries)

    async def candidates_async(self, *queries: str | None) -> list[dict]:
        items = {}
        ordered = sorted((q for q in queries if q and q.strip()), key=lambda q: not ("@" in q or any(c.isdigit() for c in q)))
        for query in dict.fromkeys(v for q in ordered for v in self.variants(q)):
            for item in await self.async_gateway.search_counterparties(query):
                if item.get("id"):
                    items[item["id"]] = item
        return self.ranked(items, queries)

    def fallback_candidates(self, *queries: str | None) -> list[dict]:
        relevant = self.candidates(*queries)
        if relevant or any(queries):
            return relevant
        return [
            self._candidate(item)
            for item in self.provider.get_recent_counterparties(limit=10)
            if item.get("id") and not item.get("archived")
        ][:5]

    async def fallback_candidates_async(
        self, *queries: str | None
    ) -> list[dict]:
        relevant = await self.candidates_async(*queries)
        if relevant or any(queries):
            return relevant
        recent = await self.async_gateway.get_recent_counterparties(limit=10)
        return [
            self._candidate(item)
            for item in recent
            if item.get("id") and not item.get("archived")
        ][:5]
