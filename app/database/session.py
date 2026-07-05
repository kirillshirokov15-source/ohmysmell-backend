from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings


engine = create_async_engine(
    settings.database_url,
    echo=False,
)

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)