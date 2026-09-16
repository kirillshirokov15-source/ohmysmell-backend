from dataclasses import dataclass, field
from typing import Protocol

from app.models.customer import Customer
from app.models.sales import (
    CustomerIdentityType,
    CustomerType,
    default_customer_type,
)
from app.repositories.customer_repository import (
    CustomerRepository,
    DuplicateCustomerIdentityError,
)
from app.schemas.order import OrderCreate


class CustomerResolutionError(Exception):
    pass


class CustomerRepositoryProtocol(Protocol):
    async def find_by_identities(self, identities): ...
    async def create(self, customer_type, display_name, identities): ...
    async def add_identities(self, customer_id, identities): ...
    async def update_customer_type(self, customer_id, customer_type): ...
    async def delete_if_unreferenced(self, customer_id): ...


@dataclass(frozen=True)
class CustomerResolution:
    customer_id: int
    customer_type: CustomerType
    moysklad_counterparty_id: str | None
    identities: dict[str, list[str]] = field(default_factory=dict)
    created: bool = False


class CustomerResolutionService:
    def __init__(
        self,
        repository: CustomerRepositoryProtocol | None = None,
    ) -> None:
        self.repository = repository or CustomerRepository()

    @staticmethod
    def normalize(identity_type: CustomerIdentityType, value: str) -> str:
        normalized = value.strip()
        if identity_type == CustomerIdentityType.PHONE:
            digits = "".join(character for character in normalized if character.isdigit())
            if len(digits) == 11 and digits.startswith("8"):
                digits = f"7{digits[1:]}"
            elif len(digits) == 10:
                digits = f"7{digits}"
            return f"+{digits}" if digits else ""
        if identity_type in {
            CustomerIdentityType.INSTAGRAM,
            CustomerIdentityType.TELEGRAM,
        }:
            normalized = normalized.casefold()
            for prefix in ("https://t.me/", "http://t.me/", "t.me/", "https://instagram.com/", "https://www.instagram.com/"):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):].rstrip("/")
                    break
            return normalized.removeprefix("@")
        return normalized.casefold()

    def identities_from_order(
        self,
        order: OrderCreate,
    ) -> list[tuple[CustomerIdentityType, str, str]]:
        raw_identities = (
            (CustomerIdentityType.EMAIL, order.email),
            (CustomerIdentityType.PHONE, order.phone),
            (CustomerIdentityType.INSTAGRAM, order.instagram_username),
            (CustomerIdentityType.TELEGRAM, order.telegram),
        )
        identities = []
        for identity_type, original in raw_identities:
            if not original:
                continue
            normalized = self.normalize(identity_type, original)
            if normalized:
                identities.append((identity_type, normalized, original))
        return identities

    async def _find_by_identity(
        self,
        identity_type: CustomerIdentityType,
        value: str,
    ) -> Customer | None:
        normalized = self.normalize(identity_type, value)
        if not normalized:
            return None
        customers = await self.repository.find_by_identities(
            [(identity_type, normalized)]
        )
        return customers[0] if customers else None

    async def find_by_email(self, value: str) -> Customer | None:
        return await self._find_by_identity(CustomerIdentityType.EMAIL, value)

    async def find_by_phone(self, value: str) -> Customer | None:
        return await self._find_by_identity(CustomerIdentityType.PHONE, value)

    async def find_by_instagram(self, value: str) -> Customer | None:
        return await self._find_by_identity(CustomerIdentityType.INSTAGRAM, value)

    async def find_by_telegram(self, value: str) -> Customer | None:
        return await self._find_by_identity(CustomerIdentityType.TELEGRAM, value)

    async def resolve_email(
        self,
        email: str,
        display_name: str | None = None,
    ) -> CustomerResolution:
        if not email or "@" not in email or len(email) > 320:
            raise CustomerResolutionError("Не указан корректный email отправителя")
        email_identity = (
            CustomerIdentityType.EMAIL,
            self.normalize(CustomerIdentityType.EMAIL, email),
            email,
        )
        customers = await self.repository.find_by_identities(
            [(email_identity[0], email_identity[1])]
        )
        if customers:
            customer = customers[0]
            return CustomerResolution(
                customer_id=customer.id,
                customer_type=CustomerType(customer.customer_type),
                moysklad_counterparty_id=customer.moysklad_counterparty_id,
                identities=self._identity_values(customer),
            )
        try:
            customer = await self.repository.create(
                customer_type=CustomerType.WHOLESALE,
                display_name=display_name,
                identities=[email_identity],
            )
        except DuplicateCustomerIdentityError as error:
            customers = await self.repository.find_by_identities(
                [(email_identity[0], email_identity[1])]
            )
            if not customers:
                raise CustomerResolutionError("Повторите обработку email") from error
            customer = customers[0]
            return CustomerResolution(
                customer_id=customer.id,
                customer_type=CustomerType(customer.customer_type),
                moysklad_counterparty_id=customer.moysklad_counterparty_id,
                identities=self._identity_values(customer),
            )
        return CustomerResolution(
            customer_id=customer.id,
            customer_type=CustomerType.WHOLESALE,
            moysklad_counterparty_id=customer.moysklad_counterparty_id,
            identities={CustomerIdentityType.EMAIL.value: [email]},
            created=True,
        )

    async def discard_if_unreferenced(
        self,
        resolution: CustomerResolution,
    ) -> None:
        if resolution.created:
            await self.repository.delete_if_unreferenced(
                resolution.customer_id
            )

    @staticmethod
    def _identity_values(customer: Customer) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for identity in customer.identities:
            identity_type = CustomerIdentityType(identity.identity_type).value
            values.setdefault(identity_type, []).append(identity.original_value)
        return values

    async def resolve(self, order: OrderCreate) -> CustomerResolution:
        identities = self.identities_from_order(order)
        lookup = [(kind, normalized) for kind, normalized, _ in identities]
        customers = await self.repository.find_by_identities(lookup)

        if len(customers) > 1:
            raise CustomerResolutionError(
                "Идентификаторы заказа принадлежат разным клиентам; "
                "автоматическое объединение запрещено"
            )

        if customers:
            customer = customers[0]
            customer_type = CustomerType(customer.customer_type)

            # Existing profiles change only through authenticated manager review.

            existing = {
                (CustomerIdentityType(identity.identity_type), identity.normalized_value)
                for identity in customer.identities
            }
            new_identities = [
                identity
                for identity in identities
                if (identity[0], identity[1]) not in existing
            ]
            try:
                await self.repository.add_identities(customer.id, new_identities)
            except DuplicateCustomerIdentityError as error:
                raise CustomerResolutionError(
                    "Одна из идентичностей уже принадлежит другому клиенту"
                ) from error

            return CustomerResolution(
                customer_id=customer.id,
                customer_type=customer_type,
                moysklad_counterparty_id=customer.moysklad_counterparty_id,
            )

        customer_type = (
            CustomerType(order.customer_type)
            if order.customer_type_explicit
            else default_customer_type(order.source)
        )
        try:
            customer = await self.repository.create(
                customer_type=customer_type,
                display_name=order.customer_name,
                identities=identities,
            )
        except DuplicateCustomerIdentityError as error:
            raise CustomerResolutionError(
                "Клиент с такой идентичностью был создан параллельно; повторите запрос"
            ) from error

        return CustomerResolution(
            customer_id=customer.id,
            customer_type=customer_type,
            moysklad_counterparty_id=customer.moysklad_counterparty_id,
            created=True,
        )
