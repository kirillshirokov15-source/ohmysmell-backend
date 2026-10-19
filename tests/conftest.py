"""Unit tests cannot reach real databases or external HTTP services."""
import os
import pytest

# Replace DATABASE_URL before importing any application engine in unit runs.
# A failed mock must never display real connection arguments in a traceback.
if os.getenv("OMS_STAGING_TESTS") != "1":
    os.environ["DATABASE_URL"] = "postgresql://unit_test:unused@127.0.0.1:1/unit_test"


@pytest.fixture(autouse=True)
def block_real_io(monkeypatch, request):
    import requests
    import asyncpg
    import aiohttp
    def blocked(*args, **kwargs):
        __tracebackhide__ = True
        raise AssertionError("Real network I/O is forbidden in automated tests")
    monkeypatch.setattr(requests.Session, "request", blocked)
    original_async_request = aiohttp.ClientSession._request
    async def local_http_only(client, method, url, **kwargs):
        from urllib.parse import urlsplit
        parsed = urlsplit(str(url))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
            blocked()
        # The health/readiness test owns a loopback HTTP server. Never follow a
        # redirect from that server to a provider endpoint.
        kwargs["allow_redirects"] = False
        return await original_async_request(client, method, url, **kwargs)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", local_http_only)
    if not (request.node.get_closest_marker("staging") and os.getenv("OMS_STAGING_TESTS") == "1"):
        monkeypatch.setattr(asyncpg, "connect", blocked)
