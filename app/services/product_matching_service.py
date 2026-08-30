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

    @classmethod
    def normalized_name_variants(cls, value: str | None) -> tuple[str, ...]:
        full = cls.normalize(value)
        variants = [full] if full else []
        if value:
            parts = re.split(r"\s+/\s+", value, maxsplit=1)
            if len(parts) == 2 and re.search(r"[А-Яа-яЁё]", parts[1]):
                primary = cls.normalize(parts[0])
                if primary and primary not in variants:
                    variants.append(primary)
        return tuple(variants)

    @staticmethod
    def _similarity(query: str, candidate: str) -> float:
        full_score = SequenceMatcher(None, query, candidate).ratio()
        query_tokens = query.split()
        candidate_tokens = candidate.split()
        if not query_tokens or not candidate_tokens:
            return full_score
        window_size = min(len(query_tokens), len(candidate_tokens))
        window_score = max(
            SequenceMatcher(
                None,
                query,
                " ".join(candidate_tokens[index:index + window_size]),
            ).ratio()
            for index in range(len(candidate_tokens) - window_size + 1)
        )
        coverage = len(set(query_tokens) & set(candidate_tokens)) / len(
            set(query_tokens)
        )
        return max(full_score, window_score, coverage)

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
            if query in self.normalized_name_variants(product.get("name"))
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
            variants = self.normalized_name_variants(product.get("name"))
            score = max(
                (self._similarity(query, candidate) for candidate in variants),
                default=0,
            )
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
                max(
                    self._similarity(query, candidate)
                    for candidate in self.normalized_name_variants(
                        product.get("name")
                    )
                ),
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
