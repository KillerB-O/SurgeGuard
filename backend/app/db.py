"""Provide the async SQLAlchemy engine and request transaction boundary.

Routes use the yielded connection for all reads and writes in one transaction.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import settings

engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)


async def get_connection() -> AsyncIterator[AsyncConnection]:
    """Yield a connection whose transaction commits on success and rolls back on error.

    Keeping the transaction open across the request makes event claiming atomic.
    """
    async with engine.begin() as conn:
        yield conn
