from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings


url = make_url(settings.database_url)

query = dict(url.query)

sslmode = query.pop("sslmode", None)

url = url.set(
    drivername="postgresql+asyncpg",
    query=query,
)

connect_args = {}

if sslmode in {"require", "verify-ca", "verify-full"}:
    connect_args["ssl"] = True

elif sslmode == "disable":
    connect_args["ssl"] = False


engine = create_async_engine(
    url,
    echo=False,
    connect_args=connect_args,
)

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)