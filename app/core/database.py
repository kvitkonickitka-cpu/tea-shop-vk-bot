from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _normalize_url(url: str) -> str:
    # Railway/большинство хостингов отдают DATABASE_URL как postgres:// или
    # postgresql:// — для async-драйвера asyncpg нужен явный диалект.
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


_engine: AsyncEngine | None = (
    create_async_engine(_normalize_url(settings.database_url)) if settings.database_url else None
)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False) if _engine else None


def get_session_factory():
    if _session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    return _session_factory


async def init_models() -> None:
    if _engine is None:
        logger.warning("DATABASE_URL is not set, skipping database initialization")
        return

    # Модели должны быть импортированы до вызова, чтобы попасть в metadata.
    from app.modules.orders import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
