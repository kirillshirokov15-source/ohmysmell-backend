"""Mandatory guard at both service and transport boundaries."""
from app.config.settings import settings


class ExternalWritesDisabled(RuntimeError):
    pass


def require_external_writes() -> None:
    if not settings.external_writes_enabled or settings.environment == "staging":
        raise ExternalWritesDisabled("External writes are disabled")


def require_counterparty_creation() -> None:
    """Narrowly scoped permission: create only counterparty, never stock docs.

    External document writes stay disabled; new counterparties require
    production, a dedicated explicit feature flag and a manager-confirmed flow.
    """
    import os
    if (settings.environment != "production"
            or os.getenv("OMS_COUNTERPARTY_CREATION_ENABLED", "false").lower() != "true"):
        raise ExternalWritesDisabled("Counterparty creation is disabled")
