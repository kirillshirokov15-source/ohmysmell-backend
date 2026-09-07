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
    def blocked(*args, **kwargs):
        __tracebackhide__ = True
        raise AssertionError("Real network I/O is forbidden in automated tests")
    monkeypatch.setattr(requests.Session, "request", blocked)
    if not (request.node.get_closest_marker("staging") and os.getenv("OMS_STAGING_TESTS") == "1"):
        monkeypatch.setattr(asyncpg, "connect", blocked)
