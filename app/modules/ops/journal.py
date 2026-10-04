"""Журнал наблюдений: обёртка на вызовы внешних сервисов и замер ответа.

Как наблюдение доходит до базы. Обёртка и замер ответа кладут запись в буфер
в памяти — это ничего не стоит и не может упасть. Буфер сбрасывается в базу
после ответа на HTTP-запрос (промежуточный слой в `app/main.py`) и в
минутном пульсе, с жёстким таймаутом. База недоступна — записи ждут в
буфере до следующего раза; переполнился буфер — теряются самые старые.
Мониторинг теряет точку, бот не теряет ничего.

Что считается ошибкой сервиса, решает `classify`. Опечатка клиента в
городе — это `validation`: ошибкой сервиса она не является, и алерты её не
считают, иначе Ops-чат звенел бы от каждого «Мсква».
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

APIS = ("yookassa", "cdek", "ozon", "claude")
API_NAMES = {"yookassa": "ЮKassa", "cdek": "СДЭК", "ozon": "Ozon", "claude": "Claude"}
VALIDATION = "validation"
# Записи с такой операцией — эмуляция для проверки алертов: пульс их
# считает (алерт должен сработать), а ежедневный отчёт — нет.
EMULATION = "эмуляция"

_BUFFER: deque[dict] = deque(maxlen=2000)
_FLUSH_TIMEOUT_SECONDS = 2.0

# Сколько секунд текущий ход клиента провёл в Claude: обёртка на вызовы
# Claude прибавляет сюда, замер ответа читает.
_llm_seconds: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar(
    "ops_llm_seconds", default=None
)
# Номер заказа, к которому относятся вызовы внутри: в карточке ошибки он
# нужнее всего, а сами клиенты сервисов его не знают.
_order_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("ops_order_id", default=None)
# Когда клиент написал первое из сообщений, на которые отвечает ход.
_received_at: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "ops_received_at", default=None
)

_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
# Отказы, в которых виноват ввод клиента, а не сервис.
_VALIDATION_MARKERS = (
    "не знает города",
    "Не нашли город",
    "не вернул ни одного тарифа",
    "отказал в расчёте",
    "нет ни кода пункта, ни адреса",
    # Ozon отвечает 404 на пачку пунктов, если хоть один из них уже убран;
    # выгрузка выбрасывает пропавших и спрашивает остальных — это штатно.
    "Не найдены пункты выдачи",
)


def classify(error: BaseException) -> tuple[str, int | None]:
    """Вид ошибки и HTTP-код, если он есть."""
    import httpx

    try:
        import anthropic
    except ImportError:  # pragma: no cover — пакет есть всегда
        anthropic = None

    # Клиенты сервисов заворачивают сетевые ошибки в свои: смотрим и причину.
    for candidate in (error, error.__cause__):
        if candidate is None:
            continue
        if isinstance(candidate, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
            return "timeout", None
        if anthropic is not None and isinstance(candidate, anthropic.APITimeoutError):
            return "timeout", None
        if isinstance(candidate, httpx.RequestError):
            return "network", None
        if anthropic is not None and isinstance(candidate, anthropic.APIConnectionError):
            return "network", None

    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        match = _HTTP_STATUS.search(str(error))
        status = int(match.group(1)) if match else None
    # Ожидаемый отказ — раньше кода ответа: «Не нашли город — HTTP 400» или
    # 404 на пропавший пункт — не сбой сервиса, хотя код у них 4xx.
    if any(marker in str(error) for marker in _VALIDATION_MARKERS):
        return VALIDATION, status
    if status is not None:
        if status in (401, 403):
            return "auth", status
        if status >= 500:
            return "http_5xx", status
        if status >= 400:
            return "http_4xx", status
    return "other", status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def note_error(api: str, operation: str, error: BaseException, duration: float) -> None:
    """Запомнить сбой сервиса и написать о нём строку в лог."""
    kind, status = classify(error)
    order_id = _order_id.get()
    _BUFFER.append({
        "at": _now(), "kind": "error", "api": api, "operation": operation[:120],
        "error_kind": kind, "http_status": status,
        "duration_ms": int(duration * 1000), "order_id": order_id,
    })
    # Текст ошибки — обрезанный: в теле ответа сервиса бывают данные
    # получателя. Телефоны и почты маскирует ещё и форматтер логов.
    logger.warning(
        "Ошибка %s: %s — %s (%s)",
        API_NAMES.get(api, api), operation, kind, str(error)[:300],
        extra={
            "service": api, "operation": operation, "order_id": order_id,
            "http_status": status, "error_code": kind, "duration_ms": int(duration * 1000),
        },
    )


def note_pii_redacted(operation: str, count: int) -> None:
    """В запрос к Claude дошли телефон или почта — их сняли перед отправкой."""
    # Строка на каждое найденное значение: отчёт считает строки. Самих
    # значений здесь нет — только где нашлись.
    for _ in range(count):
        _BUFFER.append({"at": _now(), "kind": "pii_redacted", "api": "claude", "operation": operation[:120]})


def note_turn(total_seconds: float, llm_seconds: float, *, operation: str | None = None) -> None:
    """Запомнить, сколько клиент ждал ответа и сколько из этого — Claude."""
    _BUFFER.append({
        "at": _now(), "kind": "turn", "operation": operation,
        "duration_ms": int(total_seconds * 1000), "llm_ms": int(llm_seconds * 1000),
    })


def mark_received(epoch: float | None) -> None:
    """Когда клиент написал (unix-время): от него и считается ожидание."""
    try:
        _received_at.set(float(epoch) if epoch else None)
    except (TypeError, ValueError):
        _received_at.set(None)


def waited(started: float) -> float:
    """Сколько клиент ждал: от сообщения, а если оно неизвестно — от начала хода.

    Время сообщения берётся у ВК, а часы контейнера с ним расходятся на
    доли секунды — поэтому не меньше длительности самого хода.
    """
    elapsed = time.monotonic() - started
    received = _received_at.get()
    if received is None:
        return elapsed
    return min(max(elapsed, time.time() - received), 86400.0)


def finish_turn(started: float, llm: list[float]) -> None:
    """Записать ответ, который дошёл до клиента. Никогда не бросает.

    Стоит после отправки в ВК: исключение здесь выглядело бы как упавший
    ход, очередь повторила бы событие, и клиент получил бы ответ дважды.
    """
    try:
        note_turn(waited(started), llm[0])
    except Exception:
        logger.debug("Не записали замер ответа", exc_info=True)


def start_turn() -> list[float]:
    """Начать счёт времени Claude для хода; вернуть копилку."""
    holder = [0.0]
    _llm_seconds.set(holder)
    return holder


def _operation_name(operation, args, kwargs) -> str:
    if callable(operation):
        try:
            return str(operation(*args, **kwargs))
        except Exception:
            return "?"
    return operation


def watch(api: str, operation=None):
    """Обёртка на вызов внешнего сервиса: сбой — в журнал, исключение — дальше.

    Ничего не меняет в поведении вызова: исключение пробрасывается то же
    самое, результат — тот же. Вложенные обёртки (вызов СДЭКа внутри
    регистрации отправления) пишут сбой один раз — самая внутренняя.
    """

    def decorate(fn):
        name = operation or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            except Exception as error:
                if not getattr(error, "_ops_recorded", False):
                    try:
                        error._ops_recorded = True
                        note_error(api, _operation_name(name, args, kwargs), error, time.monotonic() - started)
                    except Exception:
                        logger.debug("Не записали сбой %s", api, exc_info=True)
                raise
            finally:
                if api == "claude":
                    holder = _llm_seconds.get()
                    if holder is not None:
                        holder[0] += time.monotonic() - started

        return wrapper

    return decorate


def order_scope(fn):
    """Вызовы внутри относятся к заказу: его номер попадёт в журнал сбоев.

    Номер берётся из аргумента `order_id` или `order` (объект с `.id`).
    """
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        order_id = None
        try:
            bound = signature.bind_partial(*args, **kwargs).arguments
            if bound.get("order_id") is not None:
                order_id = int(bound["order_id"])
            elif getattr(bound.get("order"), "id", None) is not None:
                order_id = int(bound["order"].id)
        except Exception:
            order_id = None
        token = _order_id.set(order_id) if order_id is not None else None
        try:
            return await fn(*args, **kwargs)
        finally:
            if token is not None:
                _order_id.reset(token)

    return wrapper


def pending() -> int:
    return len(_BUFFER)


async def flush(timeout: float = _FLUSH_TIMEOUT_SECONDS) -> int:
    """Сбросить буфер в базу. Никогда не бросает; вернуть, сколько записали."""
    if not _BUFFER:
        return 0
    from app.core.database import get_session_factory

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return 0

    rows = list(_BUFFER)
    _BUFFER.clear()
    try:
        await asyncio.wait_for(_insert(session_factory, rows), timeout)
        return len(rows)
    except Exception as error:
        # Возвращаем в начало буфера: новые записи, пришедшие за это время,
        # остаются после них, лишнее срежет maxlen.
        _BUFFER.extendleft(reversed(rows))
        logger.warning("Журнал мониторинга не записался: %s", type(error).__name__)
        return 0


async def _insert(session_factory, rows: list[dict]) -> None:
    from app.modules.ops.models import OpsEvent

    async with session_factory() as session:
        session.add_all(OpsEvent(**row) for row in rows)
        await session.commit()
