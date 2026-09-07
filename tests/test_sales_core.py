import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy import create_engine, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer import Customer, CustomerIdentity
from app.models.sales import CustomerIdentityType, CustomerType, OrderSource
from app.schemas.order import OrderCreate
from app.services.customer_resolution_service import (
    CustomerResolution,
    CustomerResolutionError,
    CustomerResolutionService,
)
from app.services.order_validation_service import OrderValidationService
from app.services.price_service import (
    PriceConfigurationError,
    PriceNotConfiguredError,
    PriceService,
)


def make_order(**overrides) -> OrderCreate:
    payload = {
        "customer_name": "Customer",
        "phone": "+7 (999) 000-00-00",
        "items": [{"id": "product-1", "qty": 1}],
    }
    payload.update(overrides)
    return OrderCreate.model_validate(payload)


def product_with_prices(*prices: tuple[str, int]) -> dict:
    return {
        "id": "product-1",
        "name": "Test product",
        "salePrices": [
            {"value": value, "priceType": {"name": name}}
            for name, value in prices
        ],
    }


class FakeProductService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.customer_type = None

    def get_catalog(self, customer_type, strict_pricing=False):
        self.customer_type = customer_type
        return self.catalog


class FakeCustomerRepository:
    def __init__(self, customers=None):
        self.customers = list(customers or [])
        self.next_id = max((customer.id for customer in self.customers), default=0) + 1

    async def find_by_identities(self, identities):
        requested = set(identities)
        return [
            customer
            for customer in self.customers
            if requested
            & {
                (CustomerIdentityType(identity.identity_type), identity.normalized_value)
                for identity in customer.identities
            }
        ]

    async def create(self, customer_type, display_name, identities):
        customer = SimpleNamespace(
            id=self.next_id,
            customer_type=customer_type,
            display_name=display_name,
            moysklad_counterparty_id=None,
            identities=[
                SimpleNamespace(
                    identity_type=kind,
                    normalized_value=normalized,
                    original_value=original,
                )
                for kind, normalized, original in identities
            ],
        )
        self.next_id += 1
        self.customers.append(customer)
        return customer

    async def add_identities(self, customer_id, identities):
        customer = next(item for item in self.customers if item.id == customer_id)
        customer.identities.extend(
            SimpleNamespace(
                identity_type=kind,
                normalized_value=normalized,
                original_value=original,
            )
            for kind, normalized, original in identities
        )

    async def update_customer_type(self, customer_id, customer_type):
        customer = next(item for item in self.customers if item.id == customer_id)
        customer.customer_type = customer_type


def existing_customer(customer_type, *identities):
    return SimpleNamespace(
        id=10,
        customer_type=customer_type,
        display_name="Existing customer",
        moysklad_counterparty_id="counterparty-1",
        identities=[
            SimpleNamespace(
                identity_type=kind,
                normalized_value=value,
                original_value=value,
            )
            for kind, value in identities
        ],
    )


class PriceServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = PriceService(
            {
                CustomerType.WHOLESALE: "Wholesale configured",
                CustomerType.RETAIL: "Retail configured",
            }
        )

    def test_price_choice_does_not_depend_on_sale_prices_order(self):
        product = product_with_prices(
            ("Unrelated first price", 99900),
            ("Retail configured", 25000),
            ("Wholesale configured", 15000),
        )
        self.assertEqual(
            self.service.get_price(product, CustomerType.WHOLESALE).value,
            15000,
        )
        self.assertEqual(
            self.service.get_price(product, CustomerType.RETAIL).value,
            25000,
        )

    def test_retail_without_retail_price_is_controlled_error(self):
        with self.assertRaisesRegex(PriceNotConfiguredError, "Retail configured"):
            self.service.get_price(
                product_with_prices(("Wholesale configured", 15000)),
                CustomerType.RETAIL,
            )

    def test_unknown_customer_type_does_not_use_first_price(self):
        with self.assertRaises(PriceConfigurationError):
            self.service.get_price(
                product_with_prices(("First", 10000)),
                CustomerType.UNKNOWN,
            )


class OrderSchemaTests(unittest.TestCase):
    def test_website_default_is_retail_and_old_payload_is_compatible(self):
        order = make_order()
        self.assertEqual(order.customer_type, CustomerType.RETAIL)
        self.assertEqual(order.source, OrderSource.WEBSITE)
        self.assertFalse(order.customer_type_explicit)

    def test_source_defaults(self):
        for source, expected in (
            ("instagram", CustomerType.RETAIL),
            ("email", CustomerType.UNKNOWN),
            ("manual", CustomerType.UNKNOWN),
        ):
            with self.subTest(source=source):
                self.assertEqual(make_order(source=source).customer_type, expected)

    def test_explicit_customer_type_has_priority(self):
        order = make_order(source="website", customer_type="wholesale")
        self.assertEqual(order.customer_type, CustomerType.WHOLESALE)
        self.assertTrue(order.customer_type_explicit)

    def test_empty_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            make_order(items=[])

    def test_frontend_price_is_ignored(self):
        order = make_order(items=[{"id": "product-1", "qty": 1, "price": 1}])
        self.assertFalse(hasattr(order.items[0], "price"))

    def test_russian_phone_8_and_plus_7_normalize_equally(self):
        service = CustomerResolutionService(FakeCustomerRepository())
        assert service.normalize(CustomerIdentityType.PHONE, "8 999 123-45-67") == (
            service.normalize(CustomerIdentityType.PHONE, "+7 999 123-45-67")
        )


class CustomerResolutionTests(unittest.TestCase):
    def resolve(self, order, repository=None):
        repository = repository or FakeCustomerRepository()
        result = asyncio.run(CustomerResolutionService(repository).resolve(order))
        return result, repository

    def test_new_customer_defaults_by_source(self):
        for source, expected in (
            ("website", CustomerType.RETAIL),
            ("instagram", CustomerType.RETAIL),
            ("email", CustomerType.UNKNOWN),
            ("manual", CustomerType.UNKNOWN),
        ):
            with self.subTest(source=source):
                result, _ = self.resolve(make_order(source=source))
                self.assertEqual(result.customer_type, expected)

    def test_existing_wholesale_through_instagram_remains_wholesale(self):
        customer = existing_customer(
            CustomerType.WHOLESALE,
            (CustomerIdentityType.INSTAGRAM, "known_shop"),
        )
        result, _ = self.resolve(
            make_order(source="instagram", instagram_username="@Known_Shop"),
            FakeCustomerRepository([customer]),
        )
        self.assertEqual(result.customer_type, CustomerType.WHOLESALE)

    def test_existing_retail_through_email_remains_retail(self):
        customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.EMAIL, "buyer@example.com"),
        )
        result, _ = self.resolve(
            make_order(source="email", email="Buyer@Example.com"),
            FakeCustomerRepository([customer]),
        )
        self.assertEqual(result.customer_type, CustomerType.RETAIL)

    def test_identity_lookup_email(self):
        customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.EMAIL, "buyer@example.com"),
        )
        found = asyncio.run(
            CustomerResolutionService(
                FakeCustomerRepository([customer])
            ).find_by_email(" BUYER@example.com ")
        )
        self.assertEqual(found.id, customer.id)

    def test_identity_lookup_instagram(self):
        customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.INSTAGRAM, "ohmysmell_customer"),
        )
        found = asyncio.run(
            CustomerResolutionService(
                FakeCustomerRepository([customer])
            ).find_by_instagram("@OhMySmell_Customer")
        )
        self.assertEqual(found.id, customer.id)

    def test_one_customer_can_have_multiple_identities(self):
        result, repository = self.resolve(
            make_order(
                email="buyer@example.com",
                instagram_username="@buyer",
                telegram="@buyer_tg",
            )
        )
        self.assertEqual(result.customer_id, repository.customers[0].id)
        self.assertEqual(len(repository.customers[0].identities), 4)

    def test_duplicate_normalized_identity_is_database_constrained(self):
        constraints = {
            constraint.name
            for constraint in CustomerIdentity.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        self.assertIn("uq_customer_identities_type_normalized", constraints)

        engine = create_engine("sqlite:///:memory:")
        Customer.__table__.create(engine)
        CustomerIdentity.__table__.create(engine)
        with Session(engine) as session:
            first = Customer(customer_type=CustomerType.RETAIL)
            second = Customer(customer_type=CustomerType.RETAIL)
            first.identities.append(
                CustomerIdentity(
                    identity_type=CustomerIdentityType.EMAIL,
                    normalized_value="buyer@example.com",
                    original_value="Buyer@example.com",
                )
            )
            second.identities.append(
                CustomerIdentity(
                    identity_type=CustomerIdentityType.EMAIL,
                    normalized_value="buyer@example.com",
                    original_value="buyer@example.com",
                )
            )
            session.add_all([first, second])
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_identities_from_different_customers_are_not_merged(self):
        email_customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.EMAIL, "buyer@example.com"),
        )
        instagram_customer = existing_customer(
            CustomerType.RETAIL,
            (CustomerIdentityType.INSTAGRAM, "different_buyer"),
        )
        instagram_customer.id = 11

        with self.assertRaises(CustomerResolutionError):
            self.resolve(
                make_order(
                    email="buyer@example.com",
                    instagram_username="@different_buyer",
                ),
                FakeCustomerRepository([email_customer, instagram_customer]),
            )


class OrderCreationTests(unittest.TestCase):
    def test_order_saves_customer_id_and_customer_type_snapshot(self):
        captured = {}

        class FakeResolutionService:
            async def resolve(self, order):
                return CustomerResolution(77, CustomerType.WHOLESALE, None)

        class FakeValidationService:
            async def validate_async(self, order):
                return {
                    "customer_name": order.customer_name,
                    "phone": order.phone,
                    "customer_type": order.customer_type,
                    "source": order.source,
                    "telegram": None,
                    "comment": None,
                    "items": [],
                    "total": 0,
                }

        async def fake_save(validated):
            captured.update(validated)
            return SimpleNamespace(id=42)

        from app.main import create_order

        with (
            patch("app.main.CustomerResolutionService", FakeResolutionService),
            patch("app.main.OrderValidationService", FakeValidationService),
            patch("app.main.save_order", fake_save),
            patch("app.main.notify_managers", AsyncMock()),
        ):
            response = asyncio.run(create_order(make_order()))

        self.assertEqual(response["order_id"], 42)
        self.assertEqual(captured["customer_id"], 77)
        self.assertEqual(captured["customer_type"], CustomerType.WHOLESALE)

    def test_telegram_failure_does_not_fail_saved_order(self):
        class FakeResolutionService:
            async def resolve(self, order):
                return CustomerResolution(78, CustomerType.RETAIL, None)

        class FakeValidationService:
            async def validate_async(self, order):
                return {
                    "customer_name": order.customer_name,
                    "phone": order.phone,
                    "customer_type": order.customer_type,
                    "source": order.source,
                    "telegram": None,
                    "comment": None,
                    "items": [],
                    "total": 0,
                }

        from app.main import create_order

        with (
            patch("app.main.CustomerResolutionService", FakeResolutionService),
            patch("app.main.OrderValidationService", FakeValidationService),
            patch(
                "app.main.save_order",
                AsyncMock(return_value=SimpleNamespace(id=43)),
            ),
            patch(
                "app.main.notify_managers",
                AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
            ),
            patch("app.main.logger.warning") as log_exception,
        ):
            response = asyncio.run(create_order(make_order()))

        self.assertTrue(response["success"])
        log_exception.assert_called_once()


class OrderValidationTests(unittest.TestCase):
    def test_duplicate_product_ids_are_combined_before_stock_check(self):
        products = FakeProductService(
            [{
                "id": "product-1",
                "name": "Test product",
                "article": "A-1",
                "price": 150,
                "total_available": 3,
            }]
        )
        validated = OrderValidationService(products).validate(
            make_order(
                customer_type="wholesale",
                source="email",
                items=[
                    {"id": "product-1", "qty": 1},
                    {"id": "product-1", "qty": 2},
                ],
            )
        )
        self.assertEqual(len(validated["items"]), 1)
        self.assertEqual(validated["items"][0]["qty"], 3)
        self.assertEqual(validated["total"], 450)


if __name__ == "__main__":
    unittest.main()
