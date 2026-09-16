from enum import StrEnum


class CustomerType(StrEnum):
    WHOLESALE = "wholesale"
    RETAIL = "retail"
    UNKNOWN = "unknown"


class OrderSource(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    INSTAGRAM = "instagram"
    WEBSITE = "website"
    MANUAL = "manual"


class CustomerIdentityType(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    INSTAGRAM = "instagram"
    TELEGRAM = "telegram"


SOURCE_CUSTOMER_TYPE_DEFAULTS = {
    OrderSource.TELEGRAM: CustomerType.UNKNOWN,
    OrderSource.WEBSITE: CustomerType.RETAIL,
    OrderSource.INSTAGRAM: CustomerType.RETAIL,
    OrderSource.EMAIL: CustomerType.WHOLESALE,
    OrderSource.MANUAL: CustomerType.UNKNOWN,
}


def default_customer_type(source: OrderSource) -> CustomerType:
    return SOURCE_CUSTOMER_TYPE_DEFAULTS[source]


def channel_customer_type(source) -> CustomerType | None:
    """An order's commercial terms are independent of its customer's profile."""
    return {OrderSource.EMAIL: CustomerType.WHOLESALE,
            OrderSource.WEBSITE: CustomerType.RETAIL}.get(source)


def order_customer_type(source, profile_type) -> CustomerType:
    return channel_customer_type(source) or CustomerType(profile_type)


def customer_type_policy(source, profile_type) -> dict:
    effective = order_customer_type(source, profile_type)
    return {"source": str(source), "profile_type": str(profile_type),
            "order_type": effective.value,
            "profile_conflict": profile_type not in {CustomerType.UNKNOWN, effective}}
