import asyncio

from app.database.base import Base
from app.database.session import engine

import app.models


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("Order tables created successfully")


if __name__ == "__main__":
    asyncio.run(main())