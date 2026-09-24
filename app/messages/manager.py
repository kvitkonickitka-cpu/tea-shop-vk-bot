"""Уведомления менеджеру: сначала запись, потом отправка.

`_notify_manager` раньше был одной попыткой: телеграм не ответил за две
секунды — и уведомление исчезало. Клиенту при этом уже сказали «уточню у
менеджера», то есть обещание осталось, а адресат о нём не узнал.

Теперь уведомление живёт в таблице. Запись коммитится первой, отправка идёт
второй, и если она не удалась, следующий тик расписания попробует снова — с
нарастающей паузой, чтобы не биться в недоступный телеграм каждые пять
минут. После десятой неудачи запись попадает в отчёт: дальше нужен человек.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import get_session_factory
from app.messages.models import ManagerNotification
from app.modules.dialog import telegram_client

logger = logging.getLogger(__name__)

# Сколько раз пробуем, прежде чем считать, что своими силами не доставим.
MAX_ATTEMPTS = 10
# Пауза удваивается с каждой попыткой, но не растёт бесконечно.
_BACKOFF_BASE_MINUTES = 1
_BACKOFF_CAP = timedelta(hours=6)
# Сколько записей отправляем за один тик: телеграм не любит очередей.
_FLUSH_LIMIT = 10

# Виды уведомлений — они же метки в логах и в отчёте.
ESCALATION = "escalation"
ESCALATION_REPING = "escalation_reping"
ORDER_CARD = "order_card"
CARRIER_FAILED = "carrier_failed"
STOREFRONT_ORDER = "storefront_order"
CLIENT_UNREACHABLE = "client_unreachable"


def _backoff(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой: удваивается, но не бесконечно.

    Показатель ограничиваем до возведения в степень: при большом числе
    попыток `2 ** attempts` перестаёт влезать в C-int, и вместо паузы
    получалось исключение прямо в обработке отказа.
    """
    steps = min(max(attempts - 1, 0), 16)
    delay = timedelta(minutes=_BACKOFF_BASE_MINUTES * (2 ** steps))
    return min(delay, _BACKOFF_CAP)


async def _send(text: str, chat_id: str | None) -> None:
    await telegram_client.send_message(text, chat_id=chat_id or None)


async def notify(
    kind: str,
    text: str,
    *,
    order_id: int | None = None,
    peer_id: int | None = None,
    chat_id: str | None = None,
) -> bool:
    """Поставить уведомление в очередь и попытаться отправить сразу.

    True означает, что уведомление сохранено, то есть не потеряется даже
    если отправка не удалась. False — записать не удалось (база в
    резервном режиме): мы всё равно пробуем отправить, но гарантий нет, и в
    лог уходит критическая запись.
    """
    stored: ManagerNotification | None = None
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        # Без базы очереди нет. Клиенту отвечаем всё равно — обещание лучше
        # сдержать хотя бы попыткой, — но след остаётся только в логе.
        logger.critical(
            "Уведомление менеджеру «%s» нигде не сохранено: база недоступна. "
            "Если телеграм не ответит, оно потеряется.", kind,
        )
        try:
            await _send(text, chat_id)
        except Exception:
            logger.exception("И отправить уведомление «%s» тоже не удалось", kind)
        return False

    async with session_factory() as session:
        stored = ManagerNotification(
            kind=kind, order_id=order_id, peer_id=peer_id,
            payload=text, chat_id=chat_id, attempts=0,
        )
        session.add(stored)
        await session.commit()
        await session.refresh(stored)

    await _try_send(stored.id, text, chat_id, attempts=0)
    return True


async def _try_send(notification_id: int, text: str, chat_id: str | None, attempts: int) -> bool:
    try:
        await _send(text, chat_id)
    except Exception as error:
        await _mark_failed(notification_id, attempts + 1, f"{type(error).__name__}: {error}")
        logger.warning(
            "Уведомление %s менеджеру не ушло (попытка %s): %s",
            notification_id, attempts + 1, error,
        )
        return False

    await _mark_sent(notification_id)
    return True


async def _mark_sent(notification_id: int) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    async with session_factory() as session:
        row = await session.get(ManagerNotification, notification_id)
        if row is None:
            return
        row.sent_at = datetime.now(timezone.utc)
        row.attempts = (row.attempts or 0) + 1
        row.last_error = None
        row.next_attempt_at = None
        await session.commit()


async def _mark_failed(notification_id: int, attempts: int, error: str) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    async with session_factory() as session:
        row = await session.get(ManagerNotification, notification_id)
        if row is None:
            return
        row.attempts = attempts
        row.last_error = error[:500]
        row.next_attempt_at = datetime.now(timezone.utc) + _backoff(attempts)
        await session.commit()
        if attempts >= MAX_ATTEMPTS:
            logger.error(
                "Уведомление %s менеджеру не доставлено после %s попыток: %s. "
                "Дальше нужен человек — запись попадёт в отчёт.",
                notification_id, attempts, error[:200],
            )


async def flush(limit: int = _FLUSH_LIMIT) -> dict:
    """Дослать то, что не ушло сразу. Вызывается по таймеру."""
    result = {"tried": 0, "sent": 0, "failed": 0}

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        result["skipped"] = "база недоступна"
        return result

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ManagerNotification)
                .where(
                    ManagerNotification.sent_at.is_(None),
                    ManagerNotification.attempts < MAX_ATTEMPTS,
                    (ManagerNotification.next_attempt_at.is_(None))
                    | (ManagerNotification.next_attempt_at <= now),
                )
                .order_by(ManagerNotification.created_at)
                .limit(limit)
            )
        ).scalars().all()
        pending = [(row.id, row.payload, row.chat_id, row.attempts or 0) for row in rows]

    for notification_id, text, chat_id, attempts in pending:
        result["tried"] += 1
        if await _try_send(notification_id, text, chat_id, attempts):
            result["sent"] += 1
        else:
            result["failed"] += 1

    return result


async def undelivered(limit: int = 20) -> list[ManagerNotification]:
    """Что так и не дошло: для отдельного блока в отчёте."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return []

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ManagerNotification)
                .where(
                    ManagerNotification.sent_at.is_(None),
                    ManagerNotification.attempts >= MAX_ATTEMPTS,
                )
                .order_by(ManagerNotification.created_at)
                .limit(limit)
            )
        ).scalars().all()
    return list(rows)
