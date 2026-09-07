import hmac

from fastapi import Header, HTTPException

from app.config.settings import settings


async def require_internal_api_token(
    x_internal_api_token: str | None = Header(default=None),
) -> None:
    expected = settings.internal_api_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Internal API token is not configured",
        )
    if not x_internal_api_token or not hmac.compare_digest(
        x_internal_api_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid internal API token")


async def require_debug_access(
    x_internal_api_token: str | None = Header(default=None),
) -> None:
    if not settings.debug_endpoints_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    await require_internal_api_token(x_internal_api_token)
