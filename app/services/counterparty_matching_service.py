from typing import Protocol

from app.integrations.moysklad.client import MoySkladClient


class CounterpartySearchProvider(Protocol):
    def search_counterparties(self, query: str) -> list[dict]: ...
    def get_recent_counterparties(self, limit: int = 10) -> list[dict]: ...


class CounterpartyMatchingService:
    def __init__(self, provider: CounterpartySearchProvider | None = None) -> None:
        self.provider = provider or MoySkladClient()

    def candidates(self, *queries: str | None) -> list[dict]:
        candidates: dict[str, dict] = {}
        for query in queries:
            if not query or not query.strip():
                continue
            for item in self.provider.search_counterparties(query.strip()):
                item_id = item.get("id")
                if not item_id:
                    continue
                candidates[item_id] = {
                    "id": item_id,
                    "name": item.get("name"),
                    "email": item.get("email"),
                    "phone": item.get("phone"),
                }
            if candidates:
                break
        return list(candidates.values())[:10]

    def fallback_candidates(self, *queries: str | None) -> list[dict]:
        relevant = self.candidates(*queries)
        if relevant:
            return relevant
        return [
            {
                "id": item["id"],
                "name": item.get("name"),
                "email": item.get("email"),
                "phone": item.get("phone"),
            }
            for item in self.provider.get_recent_counterparties(limit=10)
            if item.get("id")
        ][:10]
