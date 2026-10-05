"""05.10.2026: копия, стартовавшая без базы, оставалась без неё навсегда."""

from __future__ import annotations

import pytest

from app.core import database
from app.core.config import settings


@pytest.fixture
def broken(monkeypatch):
    """Копия в резервном режиме — как после старта в минуту сетевого сбоя."""
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    monkeypatch.setattr(database, "_down_since", 0.0)
    monkeypatch.setattr(database, "_next_try", 0.0)


async def test_copy_without_database_reconnects(db, broken):
    assert not database.is_available() and database.down_for() is not None
    assert await database.recover()
    assert database.is_available() and database.down_for() is None
    async with database.get_session_factory()() as session:
        from sqlalchemy import text

        assert (await session.execute(text("select 1"))).scalar() == 1


async def test_failed_attempt_waits_before_the_next(db, broken, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://nobody@127.0.0.1:1/none")
    assert not await database.recover()
    # Следующая попытка — не раньше чем через _RETRY_SECONDS: без этого каждое
    # событие очереди ждало бы таймаут подключения.
    assert database._next_try > 0 and not await database.recover()
    assert not database.is_available()


async def test_health_shows_how_long_without_database(db, broken, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://nobody@127.0.0.1:1/none")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bot") as client:
        body = (await client.get("/health")).json()
    assert body["database"].startswith("резервный режим, ")
