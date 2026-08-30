from app.database.session import async_session
from app.models.email_cursor import EmailProviderCursor


class EmailCursorRepository:
    async def get(self, provider: str) -> str | None:
        async with async_session() as session:
            cursor = await session.get(EmailProviderCursor, provider)
            return cursor.cursor_value if cursor else None

    async def set(self, provider: str, value: str) -> None:
        async with async_session() as session:
            cursor = await session.get(EmailProviderCursor, provider)
            if cursor is None:
                cursor = EmailProviderCursor(provider=provider, cursor_value=value)
                session.add(cursor)
            else:
                cursor.cursor_value = value
            await session.commit()
