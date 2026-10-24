from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

# Короткий таймаут подключения. Дефолтные 60 секунд asyncpg на serverless
# не годятся: выполнение запроса ограничено 60 секундами, и контейнер успеет
# умереть раньше, чем мы поймём, что база недоступна, и перейдём в резервный
# режим. Пять секунд достаточно для живой базы в той же зоне.
_CONNECT_TIMEOUT_SECONDS = 5
# Как часто копия без базы пробует подключиться снова.
_RETRY_SECONDS = 30
# Соединения в пуле старше получаса открываем заново: сетевой путь до ВМ
# (NAT Yandex Cloud) молча рвёт долгоживущие соединения, и запрос на таком
# висел бы до таймаута. pre_ping проверяет соединение перед выдачей.
_POOL_RECYCLE_SECONDS = 1800


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


def _create_engine() -> AsyncEngine:
    return create_async_engine(
        _normalize_url(settings.database_url),
        connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS},
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
    )


_engine: AsyncEngine | None = _create_engine() if settings.database_url else None
_session_factory = async_sessionmaker(_engine, expire_on_commit=False) if _engine else None

# Без базы с какого момента (monotonic) и когда пробовать снова. Состояние —
# в памяти копии: у каждой копии контейнера своё подключение.
_down_since: float | None = None
_next_try: float = 0.0
_lock = asyncio.Lock()
_recovery: asyncio.Task | None = None


def get_session_factory():
    if _session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    return _session_factory


def is_available() -> bool:
    # False означает, что модули работают на резервном хранении в памяти.
    return _session_factory is not None


def down_for() -> float | None:
    """Сколько секунд эта копия работает без базы — или None."""
    return None if _down_since is None else time.monotonic() - _down_since


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
    # Признак работы пункта и его тип: закрытый пункт Ozon из каталога не
    # убирает, а предлагать его клиенту нельзя.
    "ALTER TABLE ozon_delivery_points ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    "ALTER TABLE ozon_delivery_points ADD COLUMN IF NOT EXISTS kind VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ozon_posting VARCHAR",
    # Номер прохода выгрузки: по нему видно пункты, которые Ozon перестал
    # отдавать, — сам он об их исчезновении не сообщает.
    "ALTER TABLE ozon_delivery_points ADD COLUMN IF NOT EXISTS seen_pass INTEGER",
    "ALTER TABLE ozon_delivery_points ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
    "ALTER TABLE ozon_delivery_points ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
    "ALTER TABLE ozon_sync_state ADD COLUMN IF NOT EXISTS pass_number INTEGER DEFAULT 1",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_id VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS receipt_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS details JSONB",
    # Напоминания о неоплаченном счёте: по одной отметке на касание, иначе
    # клиент получал бы их каждые пять минут вместе с тиком расписания.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS reminder_1_sent_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS reminder_2_sent_at TIMESTAMPTZ",
    # Контроль ответа на вопрос клиента: напоминание менеджеру и сообщение
    # клиенту уходят по одному разу, и отметки об этом нужны именно здесь.
    "ALTER TABLE escalations ADD COLUMN IF NOT EXISTS reping_sent_at TIMESTAMPTZ",
    "ALTER TABLE escalations ADD COLUMN IF NOT EXISTS client_ping_sent_at TIMESTAMPTZ",
    # Резервный канал для недоставленных уведомлений менеджеру.
    "ALTER TABLE manager_notifications ADD COLUMN IF NOT EXISTS fallback_sent_at TIMESTAMPTZ",
    # Судьба посылки у перевозчика: последний статус, когда спрашивали и три
    # отметки событий. От «вручено» зависит закрывающий чек.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS carrier_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS carrier_checked_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS handed_over_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS not_delivered_at TIMESTAMPTZ",
    # Сборка со сканированием кодов маркировки.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_by VARCHAR",
    # Закрывающий чек при вручении.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_receipt_id VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_receipt_status VARCHAR",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_receipt_attempt INTEGER",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_receipt_sent_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS settlement_note VARCHAR",
    # Продающая часть: когда пришли деньги, «в пункте выдачи», срок хранения.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS at_pickup_at TIMESTAMPTZ",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS storage_until DATE",
    # Короткий путь до оплаты: снимок того, за что выставлен каждый счёт.
    "ALTER TABLE order_payments ADD COLUMN IF NOT EXISTS snapshot JSONB",
    # Кнопки ВК: что умеет приложение клиента.
    "ALTER TABLE client_preferences ADD COLUMN IF NOT EXISTS client_info JSONB",
    "ALTER TABLE client_preferences ADD COLUMN IF NOT EXISTS last_offer_buttons JSONB",
    # Повторные касания: клиент запретил сообщения — продающих не шлём.
    "ALTER TABLE client_preferences ADD COLUMN IF NOT EXISTS unreachable_at TIMESTAMPTZ",
    "ALTER TABLE client_preferences ADD COLUMN IF NOT EXISTS fresh_since TIMESTAMPTZ",
    # Аналитика: тестовые заказы не попадают в представления для DataLens.
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE funnel_events ADD COLUMN IF NOT EXISTS source VARCHAR",
    # Сессия диалога (dialog_sessions): рубли Claude на диалог и время между
    # шагами воронки. Источник клиента — first touch и метка «свои».
    "ALTER TABLE funnel_events ADD COLUMN IF NOT EXISTS dialog_id INTEGER",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS first_source VARCHAR",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS own_at TIMESTAMPTZ",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS own_by VARCHAR",
    # Расход Claude по диалогам: чей ход, на каком этапе, для чего.
    "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS client_key VARCHAR",
    "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS dialog_id INTEGER",
    "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS order_id INTEGER",
    "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS stage VARCHAR",
    "ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS purpose VARCHAR",
    # Отзывы (C7): оценка 1–5 рядом с прежней great/ok/no, публикация отзыва.
    "ALTER TABLE order_ratings ADD COLUMN IF NOT EXISTS score INTEGER",
    "ALTER TABLE order_feedback ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
    "ALTER TABLE order_feedback ADD COLUMN IF NOT EXISTS published_how VARCHAR",
    # Попытки оплаты: способ и сколько получит магазин — из уведомлений ЮKassa.
    "ALTER TABLE order_payments ADD COLUMN IF NOT EXISTS payment_method VARCHAR",
    "ALTER TABLE order_payments ADD COLUMN IF NOT EXISTS income_amount NUMERIC(10, 2)",
    "ALTER TABLE order_payments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    # Мониторинг: какие сбои уже ушли в Ops подробностью.
    "ALTER TABLE ops_events ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ",
    "ALTER TABLE ops_events ADD COLUMN IF NOT EXISTS data JSONB",
)


# Коды заказа переехали из пула `marking_codes` в свою таблицу
# `order_marking_codes` (16.10.2026) — переносим прежние привязки к заказам.
#
# Не строкой в `_MISSING_COLUMNS`: те идут в одной транзакции со схемой, и
# ошибка одной строки в Postgres обрывает всю транзакцию — бот ушёл бы в
# резервный режим. Перенос — своей транзакцией после схемы (`_move_marking`).
#
# Повторный запуск ничего не задваивает: пара GTIN + серийный номер,
# уже известная новой таблице (в любом статусе), пропускается, а ON CONFLICT
# страхует от гонки двух контейнеров, стартующих разом.
_MARKING_TRANSFER = (
    "INSERT INTO order_marking_codes (order_id, item_index, raw_code, gtin, serial, kind, "
    "status, from_pool, manual, scanned_at, scanned_by, sold_at) "
    "SELECT m.order_id, COALESCE(m.item_index, 0), m.code, m.gtin, m.serial, 'pack', "
    "CASE m.status WHEN 'assigned' THEN 'scanned' ELSE m.status END, m.from_pool, "
    "m.manual, COALESCE(m.scanned_at, m.created_at), m.scanned_by, m.sold_at "
    "FROM marking_codes m WHERE m.order_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM order_marking_codes o WHERE o.gtin = m.gtin AND o.serial = m.serial) "
    "ON CONFLICT DO NOTHING"
)
# Сверка в той же транзакции: после переноса у каждой старой привязки есть
# строка в новой таблице. Не сошлось — откат, база как была.
_MARKING_TRANSFER_CHECK = (
    "SELECT count(*) FROM marking_codes m WHERE m.order_id IS NOT NULL AND NOT EXISTS ("
    "SELECT 1 FROM order_marking_codes o WHERE o.gtin = m.gtin AND o.serial = m.serial)"
)


async def _move_marking(engine) -> int:
    """Перенести старые привязки кодов к заказам. Сколько строк перенесено.

    Одна транзакция: при любой ошибке всё откатывается, бот стартует как
    обычно (коды старых заказов просто не видны странице сборки и чеку), а в
    лог — строка с причиной; следующий старт попробует снова.
    """
    try:
        async with engine.begin() as conn:
            moved = (await conn.exec_driver_sql(_MARKING_TRANSFER)).rowcount or 0
            left = (await conn.exec_driver_sql(_MARKING_TRANSFER_CHECK)).scalar()
            if left:
                raise RuntimeError(f"после переноса без пары осталось привязок: {left}")
    except Exception:
        logger.exception("Перенос привязок кодов маркировки не удался — откатили, база как была")
        return 0
    if moved:
        logger.warning("Перенесены привязки кодов маркировки в order_marking_codes: %s", moved)
    return moved


async def _add_missing_columns(conn) -> None:
    for statement in _MISSING_COLUMNS:
        try:
            await conn.exec_driver_sql(statement)
        except Exception:
            # Не роняем старт и не уводим бота в резервный режим из-за этого:
            # без колонки он работает, просто хуже различает автора реплик.
            logger.exception("Не удалось выполнить миграцию: %s", statement)


def _import_models() -> None:
    # Модели должны быть импортированы до create_all, чтобы попасть в metadata.
    from app.modules.dialog import models as dialog_models  # noqa: F401
    from app.modules.orders import models as orders_models  # noqa: F401
    from app.modules.delivery import models as delivery_models  # noqa: F401
    from app.core import heartbeat as heartbeat_models  # noqa: F401
    from app.core import flags as flag_models  # noqa: F401
    from app.messages import models as message_models  # noqa: F401
    from app.modules.marking import models as marking_models  # noqa: F401
    from app.modules.catalog import models as catalog_models  # noqa: F401
    from app.modules.ops import models as ops_models  # noqa: F401
    from app.modules.analytics import models as analytics_models  # noqa: F401
    from app.privacy import models as privacy_models  # noqa: F401
    from app.modules.queue import models as queue_models  # noqa: F401
    from app.modules.consent import models as consent_models  # noqa: F401
    from app.modules.referral import models as referral_models  # noqa: F401


async def _connect() -> bool:
    """Подключиться и привести схему в порядок. False — база не ответила."""
    global _engine, _session_factory, _down_since

    engine = _engine or _create_engine()
    try:
        async with engine.begin() as conn:
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
            "База недоступна — работаем в резервном режиме: история диалогов и "
            "черновики заказов живут только в памяти. Пробуем подключиться снова "
            "каждые %s с.", _RETRY_SECONDS,
        )
        await engine.dispose()
        _engine = None
        _session_factory = None
        if _down_since is None:
            _down_since = time.monotonic()
        return False

    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _move_marking(engine)
    if _down_since is not None:
        logger.warning("База снова доступна — резервный режим длился %.0f с", time.monotonic() - _down_since)
    _down_since = None

    # Схема для DataLens — отдельной транзакцией: если представления не
    # пересоздались, бот работает как работал, а не уходит в резервный режим.
    from app.modules.analytics import views as analytics_views

    try:
        async with engine.begin() as conn:
            await analytics_views.create(conn)
    except Exception:
        logger.exception("Не пересоздали схему analytics для DataLens")
    # Реестр ЮKassa и витрина КУДиР в схеме finance — тоже отдельно: без них
    # бот работает, нет только сверки выплат.
    from app.modules.payment import registry as yk_registry

    try:
        async with engine.begin() as conn:
            await yk_registry.ensure_tables(conn)
    except Exception:
        logger.exception("Не создали таблицы реестра ЮKassa в схеме finance")
    # Каталог в схеме shop — тоже отдельно: при CATALOG_SOURCE=sheet бот без
    # неё работает, а при db без неё бот сам скажет о паузе (`catalog.store`).
    from app.modules.catalog import schema as catalog_schema

    from app.modules.marking import schema as marking_schema
    from app.modules.stock import schema as stock_schema

    try:
        async with engine.begin() as conn:
            await catalog_schema.ensure(conn)
            await stock_schema.ensure(conn)
            await marking_schema.ensure(conn)
    except Exception:
        logger.exception("Не создали схему каталога и склада shop")
    return True


async def init_models() -> None:
    if not settings.database_url:
        logger.warning("DATABASE_URL is not set, skipping database initialization")
        return
    _import_models()
    if not await _connect():
        _start_recovery()


async def recover() -> bool:
    """Копия без базы — попробовать подключиться снова. True — база есть.

    05.10.2026 копия контейнера, стартовавшая в минуту сетевого сбоя, ушла в
    резервный режим и осталась в нём навсегда: прежний код подключался один
    раз, при старте. Почти час история и заказы жили только в памяти, хотя
    база давно ответила бы, — и «снова отвечает» в Ops не пришло. Теперь
    попытка идёт не чаще раза в `_RETRY_SECONDS`: из фонового цикла, пульса,
    тика расписания и /health. Когда база есть — это одна проверка флага.
    """
    global _next_try
    if _session_factory is not None or not settings.database_url:
        return _session_factory is not None
    if time.monotonic() < _next_try:
        return False
    async with _lock:
        if _session_factory is not None:
            return True
        _next_try = time.monotonic() + _RETRY_SECONDS
        _import_models()
        return await _connect()


async def _recovery_loop() -> None:
    while _session_factory is None and settings.database_url:
        await asyncio.sleep(_RETRY_SECONDS)
        try:
            await recover()
        except Exception:
            logger.exception("Повторное подключение к базе упало")


def _start_recovery() -> None:
    global _recovery
    if _recovery is None or _recovery.done():
        _recovery = asyncio.get_running_loop().create_task(_recovery_loop())
