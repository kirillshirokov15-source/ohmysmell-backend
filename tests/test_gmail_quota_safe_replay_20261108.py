"""Regression tests for durable Gmail query replay and request quotas."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from app.integrations.email.gmail_provider import GmailEmailProvider
from app.integrations.email.health import classify


class Response:
    def __init__(self, obj):
        self.obj = obj

    def execute(self):
        return self.obj


class GmailService:
    def __init__(self, ids):
        self.ids = ids
        self.get_calls = []
        self.queries = []

    def users(self):
        return self

    def getProfile(self, **kwargs):
        return Response({"historyId": "42", "emailAddress": "test@example.com"})

    def messages(self):
        return self

    def list(self, **kwargs):
        self.queries.append(kwargs.get("q"))
        return Response({"messages": [{"id": value} for value in self.ids]})


def test_processed_ids_not_redownloaded_on_restart(monkeypatch):
    monkeypatch.setenv("GMAIL_MAX_FULL_MESSAGES_PER_POLL", "20")
    ids = [f"{i:016x}" for i in range(61)]
    service = GmailService(ids)
    provider = GmailEmailProvider(service=service, order_query="subject:order")
    fetched = []
    provider._get_message = lambda svc, ident: (fetched.append(ident) or ident)

    provider.acknowledge_committed(ids[:25])
    assert provider._fetch_sync(None).messages == ids[25:45]
    assert len(fetched) == 20
    assert all(q is None or q.startswith("in:inbox (") for q in service.queries)

    for ident in fetched:
        provider.acknowledge(ident)
    assert provider._fetch_sync(None).messages == ids[45:]
    assert len(fetched) == 36
    assert provider._fetch_sync(None).messages == []


def test_uncommitted_message_remains_eligible(monkeypatch):
    monkeypatch.setenv("GMAIL_MAX_FULL_MESSAGES_PER_POLL", "20")
    ids = ["a", "b", "c"]
    provider = GmailEmailProvider(service=GmailService(ids), order_query="from:shop")
    provider._get_message = lambda svc, ident: ident
    provider.acknowledge_committed(["a", "c"])
    assert provider._fetch_sync(None).messages == ["b"]


def test_gmail_limit_refuses_unsafe_values(monkeypatch):
    monkeypatch.setenv("GMAIL_MAX_FULL_MESSAGES_PER_POLL", "200")
    provider = GmailEmailProvider(service=GmailService(["a"]), order_query="from:shop")
    with pytest.raises(ValueError, match="between 1 and 50"):
        provider._fetch_sync(None)


def test_403_quota_error_is_not_bad_oauth():
    from googleapiclient.errors import HttpError
    from httplib2 import Response as HttpResponse
    error = HttpError(HttpResponse({"status": 403}),
        json.dumps({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}).encode())
    assert classify(error) == "rate_limited"


def test_plain_403_remains_auth_error():
    from googleapiclient.errors import HttpError
    from httplib2 import Response as HttpResponse
    error = HttpError(HttpResponse({"status": 403}),
        json.dumps({"error": {"errors": [{"reason": "forbidden"}]}}).encode())
    assert classify(error) == "bad_credentials"
