from typing import Protocol

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
        }

    def candidates(self, *queries: str | None) -> list[dict]:
        candidates: dict[str, dict] = {}
        for query in queries:
            if not query or not query.strip():
                continue
            for item in self.provider.search_counterparties(query.strip()):
                item_id = item.get("id")
                if not item_id:
                    continue
                candidates[item_id] = self._candidate(item)
            if candidates:
                break
        return list(candidates.values())[:10]

    async def candidates_async(self, *queries: str | None) -> list[dict]:
        candidates: dict[str, dict] = {}
        for query in queries:
            if not query or not query.strip():
                continue
            items = await self.async_gateway.search_counterparties(query.strip())
            for item in items:
                item_id = item.get("id")
                if item_id:
                    candidates[item_id] = self._candidate(item)
            if candidates:
                break
        return list(candidates.values())[:10]

    def fallback_candidates(self, *queries: str | None) -> list[dict]:
        relevant = self.candidates(*queries)
        if relevant:
            return relevant
        return [
            self._candidate(item)
            for item in self.provider.get_recent_counterparties(limit=10)
            if item.get("id")
        ][:10]

    async def fallback_candidates_async(
        self, *queries: str | None
    ) -> list[dict]:
        relevant = await self.candidates_async(*queries)
        if relevant:
            return relevant
        recent = await self.async_gateway.get_recent_counterparties(limit=10)
        return [
            self._candidate(item)
            for item in recent
            if item.get("id")
        ][:10]
