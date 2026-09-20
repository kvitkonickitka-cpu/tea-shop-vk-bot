from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

# Короткий таймаут подключения. Дефолтные 60 секунд asyncpg на serverless
# не годятся: выполнение запроса ограничено 60 секундами, и контейнер успеет
# умереть раньше, чем мы поймём, что база недоступна, и перейдём в резервный
# режим. Пять секунд достаточно для живой базы в той же зоне.
_CONNECT_TIMEOUT_SECONDS = 5


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
    create_async_engine(
        _normalize_url(settings.database_url),
        connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS},
    )
    if settings.database_url
    else None
)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False) if _engine else None


def get_session_factory():
    if _session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    return _session_factory


def is_available() -> bool:
    # False означает, что модули работают на резервном хранении в памяти.
    return _session_factory is not None


# Бедняцкие миграции: create_all создаёт недостающие таблицы, но не добавляет
# колонки в уже существующие, а Alembic в проекте нет. Операции идемпотентны и
# стоят поиск по системному каталогу, так что переживают запуск на каждом
# холодном старте. Если появится третья такая строчка — пора заводить Alembic.
_MISSING_COLUMNS = (
    "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS author VARCHAR",
    # Получатель, код пункта выдачи и выбранный тариф — всё, что нужно СДЭКу
    # для регистрации заказа. Одной колонкой, чтобы не плодить миграции.
    "ALTER TABLE order_drafts ADD COLUMN IF NOT EXISTS details JSONB",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cdek_uuid VARCHAR",
)


async def _add_missing_columns(conn) -> None:
    for statement in _MISSING_COLUMNS:
        try:
            await conn.exec_driver_sql(statement)
        except Exception:
            # Не роняем старт и не уводим бота в резервный режим из-за этого:
            # без колонки он работает, просто хуже различает автора реплик.
            logger.exception("Не удалось выполнить миграцию: %s", statement)


async def init_models() -> None:
    global _engine, _session_factory

    if _engine is None:
        logger.warning("DATABASE_URL is not set, skipping database initialization")
        return

    # Модели должны быть импортированы до вызова, чтобы попасть в metadata.
    from app.modules.dialog import models as dialog_models  # noqa: F401
    from app.modules.orders import models as orders_models  # noqa: F401
    from app.modules.delivery import models as delivery_models  # noqa: F401

    try:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _add_missing_columns(conn)
    except Exception:
        # Недоступная база раньше роняла старт целиком: uvicorn завершался,
        # контейнер уходил в цикл перезапусков и переставал отвечать вообще
        # на что-либо, включая /health. Вместо этого гасим подключение —
        # get_session_factory() начнёт бросать RuntimeError, а резервное
        # хранение в памяти, уже написанное в history/state/service, включится
        # само, без изменений в этих модулях.
        logger.exception(
            "База недоступна — запускаемся в резервном режиме. История диалогов "
            "и черновики заказов будут жить только в памяти процесса и пропадут "
            "при перезапуске. Подключение восстановится при следующем запуске, "
            "когда база снова станет доступна."
        )
        await _engine.dispose()
        _engine = None
        _session_factory = None
