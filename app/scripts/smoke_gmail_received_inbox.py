"""Offline regression: Gmail inbox order must use received_at before pagination."""
import ast
import asyncio
from pathlib import Path
from sqlalchemy.dialects.postgresql import dialect
import app.repositories.draft_order_repository as repository_module


class FakeResult:
    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return []


class FakeSession:
    def __init__(self):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, query):
        self.queries.append(query)
        return FakeResult()


async def check_queries():
    original = repository_module.async_session
    observed = []
    try:
        def fake_sessions():
            session = FakeSession()
            observed.append(session)
            return session

        repository_module.async_session = fake_sessions
        for kind in ("orders", "questions", "technical"):
            rows = await repository_module.DraftOrderRepository().list(
                limit=10, offset=20, source="email", email_kind=kind,
                sort_by_received_at=True,
            )
            assert rows == []
            assert len(observed[-1].queries) == 1
            query = observed[-1].queries[0]
            sql = str(query.compile(dialect=dialect()))
            assert "JOIN inbound_messages" in sql, sql
            assert "ORDER BY inbound_messages.received_at DESC, draft_orders.id DESC" in sql, sql
            assert " LIMIT " in sql and " OFFSET " in sql, sql
            assert "contact_details" in sql
        await repository_module.DraftOrderRepository().list(
            source="email", email_kind="orders",
            sort_by_received_at=False,
        )
        legacy = str(observed[-1].queries[0].compile(dialect=dialect()))
        assert "ORDER BY draft_orders.created_at DESC, draft_orders.id DESC" in legacy
        assert "JOIN inbound_messages" not in legacy
        try:
            await repository_module.DraftOrderRepository().list(
                source="website", sort_by_received_at=True,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Must not apply Gmail sorting to non-email sources")
    finally:
        repository_module.async_session = original


def main():
    for path in ("app/repositories/draft_order_repository.py",
                 "app/bot/telegram_bot.py"):
        ast.parse(Path(path).read_text(encoding="utf-8-sig"), filename=path)
    asyncio.run(check_queries())
    bot = Path("app/bot/telegram_bot.py").read_text(encoding="utf-8-sig")
    assert "OMS_EMAIL_SOURCE_DATE_SORT_ENABLED" in bot
    assert "draftpage:email_questions:0" in bot
    assert 'strftime("%d.%m")' in bot
    print("GMAIL_RECEIVED_DATE_INBOX_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
