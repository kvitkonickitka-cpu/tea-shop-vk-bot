"""Общая обвязка тестов.

Настройки приложения читаются при импорте, а движок базы создаётся там же,
поэтому переменные окружения выставляются здесь — до того, как тесты
потянут `app.*`.

База нужна по-настоящему: без неё бот уходит в резервное хранение в памяти,
и проверки журнала отправок, outbox и черновиков проверяли бы не тот код.
Адрес берётся из `TEST_DATABASE_URL`; если базы нет, тесты с фикстурой `db`
пропускаются, а остальные работают.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@/tea_test?host=/tmp&port=5433"),
)
os.environ.setdefault("VK_ACCESS_TOKEN", "test-vk-token")
os.environ.setdefault("VK_GROUP_ID", "club240363526")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("TELEGRAM_MANAGER_CHAT_ID", "1")
os.environ.setdefault("YOOKASSA_SHOP_ID", "1475067")
os.environ.setdefault("YOOKASSA_SECRET_KEY", "test-yookassa-key")
os.environ.setdefault("PAYMENTS_ENABLED", "true")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


_initialized = False


def _rebuild_engine():
    """Движок без пула соединений.

    pytest-asyncio даёт каждому тесту свой цикл событий, а соединения в пуле
    остаются привязанными к тому, в котором открылись: второй тест получал
    «another operation is in progress». NullPool открывает соединение заново
    на каждый запрос — для тестов это дешевле, чем возиться с циклами.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core import database
    from app.core.config import settings

    database._engine = create_async_engine(
        database._normalize_url(settings.database_url), poolclass=NullPool
    )
    database._session_factory = async_sessionmaker(database._engine, expire_on_commit=False)


@pytest.fixture
async def db():
    """Живая база с созданными таблицами. Нет базы — тест пропускается.

    Фикстура на каждый тест, а не на сессию: у pytest-asyncio сессионная
    асинхронная фикстура живёт в своём цикле событий, и соединение из неё
    в тесте падает на «another operation is in progress».
    """
    global _initialized
    from app.core import database

    if database._engine is None:
        pytest.skip("DATABASE_URL не задан")

    _rebuild_engine()

    if not _initialized:
        try:
            await database.init_models()
        except Exception as error:  # база недоступна — это не провал теста
            pytest.skip(f"база недоступна: {type(error).__name__}")
        _initialized = True
        _rebuild_engine()

    if database._session_factory is None:
        pytest.skip("база недоступна, приложение ушло в память")

    return database.get_session_factory()


@pytest.fixture
async def clean(db):
    """Пустые таблицы перед тестом: проверки должны быть независимыми."""
    from sqlalchemy import text

    wanted = (
        "orders", "order_drafts", "conversation_messages", "conversations",
        "escalations", "escalation_states", "client_notices",
        "manager_notifications", "processed_events", "dialog_reports",
        "heartbeats",
    )
    async with db() as session:
        rows = (
            await session.execute(
                text(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'public'"
                )
            )
        ).scalars().all()
        existing = [name for name in wanted if name in set(rows)]
        if existing:
            await session.execute(
                text(f"truncate {', '.join(existing)} restart identity cascade")
            )
            await session.commit()
    return db
