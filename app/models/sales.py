from enum import StrEnum


class CustomerType(StrEnum):
    WHOLESALE = "wholesale"
    RETAIL = "retail"
    UNKNOWN = "unknown"


class OrderSource(StrEnum):
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
    OrderSource.WEBSITE: CustomerType.RETAIL,
    OrderSource.INSTAGRAM: CustomerType.RETAIL,
    OrderSource.EMAIL: CustomerType.UNKNOWN,
    OrderSource.MANUAL: CustomerType.UNKNOWN,
}


def default_customer_type(source: OrderSource) -> CustomerType:
    return SOURCE_CUSTOMER_TYPE_DEFAULTS[source]
