import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from app.integrations.moysklad.client import MoySkladClient
from app.models.draft_order import ProductMatchStatus


class ProductCatalogProvider(Protocol):
    def get_products(self) -> list[dict]: ...


@dataclass(frozen=True)
class ProductCandidate:
    id: str
    name: str
    article: str | None
    score: float


@dataclass(frozen=True)
class ProductMatch:
    raw_product_text: str
    qty: int
    status: ProductMatchStatus
    product: dict | None
    candidates: tuple[ProductCandidate, ...]


class ProductMatchingService:
    def __init__(
        self,
        provider: ProductCatalogProvider | None = None,
        fuzzy_threshold: float = 0.72,
        candidate_limit: int = 5,
    ) -> None:
        self.provider = provider or MoySkladClient()
        self.fuzzy_threshold = fuzzy_threshold
        self.candidate_limit = candidate_limit
        self._products: list[dict] | None = None

    @staticmethod
    def normalize(value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))

    def products(self) -> list[dict]:
        if self._products is None:
            self._products = [
                product
                for product in self.provider.get_products()
                if not product.get("archived")
            ]
        return self._products

    def match(self, raw_product_text: str, qty: int) -> ProductMatch:
        products = self.products()
        query = self.normalize(raw_product_text)

        article_matches = [
            product
            for product in products
            if self.normalize(product.get("article")) == query
        ]
        if len(article_matches) == 1:
            return ProductMatch(
                raw_product_text, qty, ProductMatchStatus.MATCHED,
                article_matches[0], (),
            )
        if len(article_matches) > 1:
            return self._ambiguous(raw_product_text, qty, article_matches, query)

        name_matches = [
            product
            for product in products
            if self.normalize(product.get("name")) == query
        ]
        if len(name_matches) == 1:
            return ProductMatch(
                raw_product_text, qty, ProductMatchStatus.MATCHED,
                name_matches[0], (),
            )
        if len(name_matches) > 1:
            return self._ambiguous(raw_product_text, qty, name_matches, query)

        scored = []
        for product in products:
            candidate_name = self.normalize(product.get("name"))
            score = SequenceMatcher(None, query, candidate_name).ratio()
            if score >= self.fuzzy_threshold:
                scored.append((score, product))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return ProductMatch(
                raw_product_text, qty, ProductMatchStatus.NOT_FOUND, None, ()
            )
        candidates = tuple(
            self._candidate(product, score)
            for score, product in scored[: self.candidate_limit]
        )
        return ProductMatch(
            raw_product_text, qty, ProductMatchStatus.AMBIGUOUS, None, candidates
        )

    def _ambiguous(self, raw_text, qty, products, query) -> ProductMatch:
        candidates = tuple(
            self._candidate(
                product,
                SequenceMatcher(
                    None, query, self.normalize(product.get("name"))
                ).ratio(),
            )
            for product in products[: self.candidate_limit]
        )
        return ProductMatch(
            raw_text, qty, ProductMatchStatus.AMBIGUOUS, None, candidates
        )

    @staticmethod
    def _candidate(product: dict, score: float) -> ProductCandidate:
        return ProductCandidate(
            id=product["id"],
            name=product.get("name") or "",
            article=product.get("article"),
            score=round(score, 4),
        )
