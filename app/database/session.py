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

# Bound lost-network waits as well as pool acquisition. A PostgreSQL-side
# statement timeout cannot stop a client waiting on a silently broken socket.
connect_args = {"timeout": 10, "command_timeout": 30}

if sslmode in {"require", "verify-ca", "verify-full"}:
    connect_args["ssl"] = True

elif sslmode == "disable":
    connect_args["ssl"] = False


engine = create_async_engine(
    url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_timeout=15,
    connect_args=connect_args,
)

async_session = async_sessionmaker(
    engine,
    expire_on_commit=False,
)
