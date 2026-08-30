from collections.abc import Sequence

from sqlalchemy import delete, exists, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database.session import async_session
from app.models.customer import Customer, CustomerIdentity
from app.models.draft_order import DraftOrder
from app.models.inbound_message import InboundMessage
from app.models.order import Order
from app.models.sales import CustomerIdentityType, CustomerType


class DuplicateCustomerIdentityError(Exception):
    pass


class CustomerRepository:
    async def delete_if_unreferenced(self, customer_id: int) -> bool:
        async with async_session() as session:
            referenced = await session.scalar(
                select(
                    or_(
                        exists().where(InboundMessage.customer_id == customer_id),
                        exists().where(DraftOrder.customer_id == customer_id),
                        exists().where(Order.customer_id == customer_id),
                    )
                )
            )
            if referenced:
                return False
            result = await session.execute(
                delete(Customer).where(Customer.id == customer_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def find_by_identities(
        self,
        identities: Sequence[tuple[CustomerIdentityType, str]],
    ) -> list[Customer]:
        if not identities:
            return []

        async with async_session() as session:
            result = await session.execute(
                select(Customer)
                .join(CustomerIdentity)
                .options(selectinload(Customer.identities))
                .where(
                    tuple_(
                        CustomerIdentity.identity_type,
                        CustomerIdentity.normalized_value,
                    ).in_(identities)
                )
            )
            return list(result.scalars().unique().all())

    async def create(
        self,
        customer_type: CustomerType,
        display_name: str | None,
        identities: Sequence[
            tuple[CustomerIdentityType, str, str]
        ],
    ) -> Customer:
        async with async_session() as session:
            customer = Customer(
                customer_type=customer_type,
                display_name=display_name,
            )
            customer.identities.extend(
                CustomerIdentity(
                    identity_type=identity_type,
                    normalized_value=normalized,
                    original_value=original,
                )
                for identity_type, normalized, original in identities
            )
            session.add(customer)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise DuplicateCustomerIdentityError from error
            await session.refresh(customer)
            return customer

    async def add_identities(
        self,
        customer_id: int,
        identities: Sequence[
            tuple[CustomerIdentityType, str, str]
        ],
    ) -> None:
        if not identities:
            return

        async with async_session() as session:
            session.add_all(
                CustomerIdentity(
                    customer_id=customer_id,
                    identity_type=identity_type,
                    normalized_value=normalized,
                    original_value=original,
                )
                for identity_type, normalized, original in identities
            )
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise DuplicateCustomerIdentityError from error

    async def update_customer_type(
        self,
        customer_id: int,
        customer_type: CustomerType,
    ) -> None:
        async with async_session() as session:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                return
            customer.customer_type = customer_type
            await session.commit()
