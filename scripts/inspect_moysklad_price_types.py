"""Read-only diagnostic for MoySklad product price type names."""

from collections import Counter
import os
import ssl

import certifi
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter


BASE_URL = "https://api.moysklad.ru/api/remap/1.2"


class SSLContextAdapter(HTTPAdapter):
    def __init__(self, ssl_context: ssl.SSLContext):
        self.ssl_context = ssl_context
        super().__init__()

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)


def verified_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    if hasattr(ssl, "enum_certificates"):
        for certificate, encoding, _trust in ssl.enum_certificates("ROOT"):
            if encoding == "x509_asn":
                context.load_verify_locations(
                    cadata=ssl.DER_cert_to_PEM_cert(certificate)
                )
    return context


def main() -> None:
    load_dotenv()
    token = os.getenv("MOYSKLAD_TOKEN")
    if not token:
        raise RuntimeError("MOYSKLAD_TOKEN is not configured")

    session = requests.Session()
    session.mount("https://", SSLContextAdapter(verified_context()))
    response = session.get(
        f"{BASE_URL}/entity/product",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "Accept": "application/json;charset=utf-8",
        },
        params={"limit": 1000},
        timeout=60,
    )
    response.raise_for_status()
    products = response.json().get("rows", [])

    counts: Counter[str] = Counter()
    examples = []
    for product in products:
        price_types = sorted(
            {
                sale_price.get("priceType", {}).get("name")
                for sale_price in product.get("salePrices") or []
                if sale_price.get("priceType", {}).get("name")
            }
        )
        counts.update(price_types)
        if price_types and len(examples) < 5:
            examples.append((product.get("name"), price_types))

    print("Unique priceType.name:", sorted(counts))
    print("Product counts:", dict(sorted(counts.items())))
    print("Examples:")
    for product_name, price_types in examples:
        print(f"- {product_name}: {price_types}")


if __name__ == "__main__":
    main()
