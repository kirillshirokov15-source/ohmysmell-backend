"""Read-only, no-network smoke for production Gmail quota fix."""
import ast
import asyncio
import json
from pathlib import Path

from httplib2 import Response as HttpResponse
from googleapiclient.errors import HttpError

from app.integrations.email.gmail_provider import GmailEmailProvider
from app.integrations.email.health import classify
from app.integrations.email.provider import EmailFetchBatch


class Response:
    def __init__(self, obj):
        self.obj = obj

    def execute(self):
        return self.obj


class Gmail:
    def __init__(self, ids):
        self.ids = ids

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, **kw):
        return Response({"historyId": "42", "emailAddress": "fake@example.com"})

    def list(self, **kw):
        return Response({"messages": [{"id": id_} for id_ in self.ids]})


class FakeCursor:
    async def get(self, name):
        return None


class FakeScalars:
    def all(self):
        return ["already-processed"]


class FakeSession:
    calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def scalars(self, query):
        self.calls += 1
        return FakeScalars()


class WorkerProvider:
    mailbox_role = "customer"
    order_query = "subject:order"

    def __init__(self):
        self.seeded = None

    def acknowledge_committed(self, values):
        self.seeded = values

    async def fetch_unprocessed(self, cursor):
        assert self.seeded == ["already-processed"]
        return EmailFetchBatch([], None)


async def verify_worker():
    import app.database.session as session_mod
    import app.workers.email_ingestion as ingestion
    original = session_mod.async_session
    try:
        fake = FakeSession()
        session_mod.async_session = lambda: fake
        provider = WorkerProvider()
        worker = ingestion.EmailIngestionWorker(
            provider=provider,
            pipeline=object(),
            cursor_repository=FakeCursor(),
        )
        assert (await worker.run_once())["received"] == 0
        assert (await worker.run_once())["received"] == 0
        assert fake.calls == 1, "Processed IDs should be seeded once per runtime"
    finally:
        session_mod.async_session = original


def main():
    for filename in ["app/integrations/email/gmail_provider.py",
                     "app/workers/email_ingestion.py",
                     "app/integrations/email/health.py"]:
        ast.parse(Path(filename).read_text(encoding="utf-8-sig"), filename=filename)

    import os
    os.environ["GMAIL_MAX_FULL_MESSAGES_PER_POLL"] = "20"
    ids = [f"{n:016x}" for n in range(61)]
    api = Gmail(ids)
    provider = GmailEmailProvider(service=api, order_query="subject:order")
    provider._get_message = lambda svc, id_: id_
    provider.acknowledge_committed(ids[:25])
    first = provider._fetch_sync(None).messages
    assert first == ids[25:45], "Only 20 unprocessed full messages are allowed"
    for value in first:
        provider.acknowledge(value)
    assert provider._fetch_sync(None).messages == ids[45:]
    assert provider._fetch_sync(None).messages == ids[45:]

    error = HttpError(HttpResponse({"status": 403}),
                      json.dumps({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}).encode())
    assert classify(error) == "rate_limited", "Quota errors must not ask for reauth"
    asyncio.run(verify_worker())
    print("GMAIL_SAFE_REPLAY_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
