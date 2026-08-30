from decimal import Decimal


MINOR_UNITS_PER_RUBLE = 100


def minor_to_major(amount_minor: int) -> Decimal:
    return Decimal(amount_minor) / Decimal(MINOR_UNITS_PER_RUBLE)


def format_rubles(amount_minor: int) -> str:
    return f"{minor_to_major(amount_minor):.2f}"
