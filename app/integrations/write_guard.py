"""Mandatory guard at both service and transport boundaries."""
from app.config.settings import settings


class ExternalWritesDisabled(RuntimeError):
    pass


def require_external_writes() -> None:
    if not settings.external_writes_enabled or settings.environment == "staging":
        raise ExternalWritesDisabled("External writes are disabled")
