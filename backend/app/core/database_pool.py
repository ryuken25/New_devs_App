import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings

logger = logging.getLogger(__name__)


def to_async_url(database_url: str) -> str:
    """Point a plain postgres URL at the asyncpg driver."""
    if database_url.startswith("postgresql+"):
        return database_url
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+asyncpg://" + database_url[len(prefix):]
    return database_url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        self._init_lock = asyncio.Lock()

    async def initialize(self):
        """Initialize database connection pool (idempotent)"""
        if self.session_factory:
            return

        async with self._init_lock:
            if self.session_factory:
                return

            database_url = to_async_url(settings.database_url)

            self.engine = create_async_engine(
                database_url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_timeout=settings.database_pool_timeout,
                pool_pre_ping=True,  # Validate connections
                pool_recycle=settings.database_pool_recycle,
                echo=False,  # Set to True for SQL debugging
            )

            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            logger.info("Database connection pool initialized")

    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
        self.engine = None
        self.session_factory = None

    @asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session from pool"""
        await self.initialize()

        session = self.session_factory()
        try:
            yield session
        finally:
            await session.close()


# Global database pool instance
db_pool = DatabasePool()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
