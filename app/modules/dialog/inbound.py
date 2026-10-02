"""Склейка сообщений, написанных подряд, и один ход на диалог.

Клиент часто пишет данные в несколько сообщений: «Иванов Иван», «89001234567»,
«ivanov@mail.ru» — и получал три ответа, два из которых просили то, что он
уже прислал. Теперь входящее сообщение сначала ложится в `inbound_messages`,
вызов ждёт тишины `message_debounce_seconds` после последнего сообщения
(но не дольше `message_debounce_max_seconds` от первого), и один ход модели
отвечает на всю пачку.

Как договариваются вызовы. Каждое событие ВК разбирает свой вызов
контейнера, иногда в разных экземплярах, поэтому всё общее — в базе:

- **блокировка диалога** — рекомендательная блокировка Postgres на peer_id.
  Кто её взял, тот и отвечает; второй ход в том же диалоге одновременно не
  начнётся;
- **хозяин хода** забирает все неотвеченные сообщения диалога, отвечает и
  только потом ставит им `done_at` и отметку «обработано» для ВК. Упал —
  отметок нет, очередь принесёт его событие снова, и повтор подберёт всю
  пачку;
- **не хозяин** — вызов, чьё сообщение забрал чужой ход или который застал
  блокировку занятой, — просто выходит. Ждать нельзя: вызов живёт не
  дольше 60 секунд, а ход — до 35;
- **остаток**: сообщения, пришедшие во время хода, хозяин после ответа
  отпускает блокировку и ставит в очередь событие `inbound_flush` — оно
  запускает следующий ход. Блокировку отпускаем ДО проверки остатка: иначе
  сообщение, которое застало блокировку занятой, могло проскочить мимо
  проверки и зависнуть;
- **страховка**: тик расписания подбирает сообщения, которые висят дольше
  минуты (хозяин умер между ответом и постановкой в очередь).

Без очереди сообщение разбирается прямо в вебхуке, а у VK на ответ восемь
секунд — там склейка выключена.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.dialog import attachments
from app.modules.dialog.models import InboundMessage

logger = logging.getLogger(__name__)

# Старшая часть ключа рекомендательной блокировки: чтобы не пересечься с
# другими блокировками по тем же числам, если они когда-нибудь появятся.
_LOCK_SPACE = 7301 << 40
_POLL_SECONDS = 0.5
# Сколько секунд из 60 вызова оставлять на отправку и отметки после хода.
_INVOCATION_SECONDS = 60
_RESERVE_SECONDS = 15
# Сообщения старше этого без хозяина подбирает тик расписания.
STALE_SECONDS = 60

# Без базы — блокировка в памяти процесса, склейки нет.
_local_locks: dict[int, asyncio.Lock] = {}

FLUSH_EVENT = "inbound_flush"


@dataclass
class Batch:
    peer_id: int
    rows: list[InboundMessage]

    @property
    def text(self) -> str:
        return "\n".join(
            (row.message.get("text") or "").strip()
            for row in self.rows
            if (row.message.get("text") or "").strip()
        )

    @property
    def merged_message(self) -> dict:
        """Одно сообщение из пачки: вложения всех — лимит фото на ход прежний."""
        merged: list = []
        for row in self.rows:
            merged.extend(row.message.get("attachments") or [])
        return {"peer_id": self.peer_id, "attachments": merged}


def is_enabled() -> bool:
    from app.modules.queue import client as queue_client

    return settings.message_debounce_seconds > 0 and queue_client.is_configured()


def _lock_key(peer_id: int) -> int:
    return _LOCK_SPACE + int(peer_id)


async def _store(event_id: str, message: dict) -> None:
    statement = insert(InboundMessage).values(
        peer_id=message["peer_id"], event_id=event_id, message=message
    ).on_conflict_do_nothing(index_elements=[InboundMessage.event_id])
    async with get_session_factory()() as session:
        await session.execute(statement)
        await session.commit()


async def _quiet_for(peer_id: int) -> tuple[float, float] | None:
    """Сколько секунд прошло после последнего и первого неотвеченного.

    Время берём у базы: сообщения записывают разные экземпляры контейнера,
    и сравнивать их часы между собой нельзя.
    """
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(
                    "select extract(epoch from now() - max(received_at)), "
                    "extract(epoch from now() - min(received_at)) "
                    "from inbound_messages where peer_id = :peer and done_at is null"
                ),
                {"peer": peer_id},
            )
        ).first()
    if row is None or row[0] is None:
        return None
    return float(row[0]), float(row[1])


async def _wait_for_quiet(peer_id: int) -> None:
    pause = settings.message_debounce_seconds
    ceiling = max(settings.message_debounce_max_seconds, pause)
    while True:
        quiet = await _quiet_for(peer_id)
        if quiet is None:
            return
        since_last, since_first = quiet
        if since_last >= pause or since_first >= ceiling:
            return
        await asyncio.sleep(min(_POLL_SECONDS, pause - since_last, ceiling - since_first) + 0.01)


async def _pending(session, peer_id: int) -> list[InboundMessage]:
    return list(
        (
            await session.execute(
                select(InboundMessage)
                .where(InboundMessage.peer_id == peer_id, InboundMessage.done_at.is_(None))
                .order_by(InboundMessage.id)
            )
        ).scalars().all()
    )


async def _finish(rows: list[InboundMessage]) -> None:
    from app.modules.dialog import service

    ids = [row.id for row in rows]
    async with get_session_factory()() as session:
        await session.execute(
            update(InboundMessage)
            .where(InboundMessage.id.in_(ids))
            .values(done_at=text("now()"))
        )
        await session.commit()
    for row in rows:
        await service.mark_processed(row.event_id)


async def _respond(batch: Batch, budget_seconds: float) -> None:
    from app.modules.dialog import service
    from app.modules.orders import buttons

    # Нажатия кнопок — кодом, по порядку, до хода модели. Что код решить не
    # может («Нет», «Изменить», старая кнопка), уходит модели текстом.
    texts: list[InboundMessage] = []
    for row in batch.rows:
        if not is_button(row.message):
            texts.append(row)
            continue
        press = await buttons.handle(batch.peer_id, row.message)
        if press.reply:
            await service.send_press_reply(
                batch.peer_id, row.message.get("text") or "", press.reply, press.keyboard,
                to_model=press.to_model,
            )
        if press.to_model:
            texts.append(row)
    if not texts:
        return
    batch = Batch(batch.peer_id, texts)

    from app.modules.ops import journal as ops_journal

    ops_journal.mark_received(_first_written(batch.rows))
    attached = await attachments.collect(batch.merged_message)
    words = batch.text
    if not words and not attached.any:
        return
    await service.respond(batch.peer_id, words, attached, budget_seconds=budget_seconds)


def _first_written(rows: list[InboundMessage]) -> float | None:
    """Когда клиент написал первое сообщение пачки — по часам ВК или по приёму.

    Только для замера мониторинга, поэтому никогда не бросает: странное поле
    `date` не должно стоить клиенту ответа.
    """
    moments = []
    for row in rows:
        try:
            written = row.message.get("date")
            if written:
                moments.append(float(written))
            elif row.received_at is not None:
                moments.append(row.received_at.timestamp())
        except (TypeError, ValueError, AttributeError):
            continue
    return min(moments) if moments else None


async def _run_turn(peer_id: int, started: float) -> bool:
    """Взять диалог и ответить на всё неотвеченное. False — диалог занят.

    Исключения хода пробрасываются: отметок «обработано» тогда нет, и
    очередь повторит событие.
    """
    from app.core import database

    key = _lock_key(peer_id)
    # Блокировка живёт на соединении, поэтому соединение своё и на весь ход:
    # сессия после коммита вернула бы его в пул, и снимать блокировку
    # пришлось бы уже на другом — она бы так и повисла.
    async with database._engine.connect() as raw:
        connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
        got = (
            await connection.execute(text("select pg_try_advisory_lock(:key)"), {"key": key})
        ).scalar()
        if not got:
            logger.info("peer_id=%s: ход уже идёт, сообщение ответит следующий", peer_id)
            return False
        try:
            async with get_session_factory()() as session:
                rows = await _pending(session, peer_id)
            if rows:
                spent = time.monotonic() - started
                budget = _INVOCATION_SECONDS - _RESERVE_SECONDS - spent
                if len(rows) > 1:
                    logger.info("peer_id=%s: склеили %s сообщений в один ход", peer_id, len(rows))
                await _respond(Batch(peer_id, rows), budget_seconds=max(budget, 10))
                await _finish(rows)
        finally:
            await connection.execute(text("select pg_advisory_unlock(:key)"), {"key": key})
    await _hand_over_leftovers(peer_id)
    return True


async def _hand_over_leftovers(peer_id: int) -> None:
    """Сообщения, пришедшие во время хода, — следующему ходу через очередь."""
    async with get_session_factory()() as session:
        left = await _pending(session, peer_id)
    if not left:
        return
    await request_flush(peer_id, left[-1].id)


async def request_flush(peer_id: int, marker: int) -> None:
    from app.modules.queue import client as queue_client

    event = {"type": FLUSH_EVENT, "peer_id": peer_id, "event_id": f"flush:{peer_id}:{marker}"}
    if queue_client.is_configured():
        try:
            await queue_client.enqueue(event)
            return
        except Exception:
            logger.exception("peer_id=%s: не поставили дообработку в очередь", peer_id)
    # Без очереди — сразу здесь: так же, как без неё разбирается и всё остальное.
    await process_pending(peer_id)


async def process_pending(peer_id: int) -> bool:
    """Ответить на неотвеченное в диалоге — без ожидания тишины."""
    return await _run_turn(peer_id, time.monotonic())


async def accept(event_id: str, message: dict, client_info: dict | None = None) -> None:
    """Принять сообщение клиента: склеить с соседними и ответить одним ходом.

    Отметку «обработано» ставит тот ход, который ответил. Если этот вызов
    хозяином не стал, отметки от него не будет — её поставит хозяин.
    """
    from app.modules.dialog import service

    started = time.monotonic()
    peer_id = message["peer_id"]

    from app.messages import keyboard as keyboards

    await keyboards.remember_client(peer_id, client_info)

    try:
        get_session_factory()
    except RuntimeError:
        # Без базы — по-старому, но всё равно по одному ходу на диалог.
        lock = _local_locks.setdefault(peer_id, asyncio.Lock())
        async with lock:
            await service.handle_message_new(message)
        if event_id:
            await service.mark_processed(event_id)
        return

    # У событий ВК event_id есть всегда; запасной ключ — на случай, если
    # сообщение пришло без него, чтобы уникальность не склеила разные.
    event_id = event_id or f"msg:{peer_id}:{message.get('conversation_message_id') or message.get('id') or time.time_ns()}"
    await _store(event_id, message)
    if is_enabled() and not is_button(message):
        await _wait_for_quiet(peer_id)
    await _run_turn(peer_id, started)


def is_button(message: dict) -> bool:
    """Нажатие кнопки: склейку не ждёт, отвечаем сразу."""
    return bool(message.get("payload"))


async def rescue_stale() -> dict:
    """Страховка для тика: диалоги с сообщениями, которые никто не взял."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {"stale": 0}
    async with session_factory() as session:
        peers = (
            await session.execute(
                text(
                    "select peer_id, max(id) from inbound_messages "
                    "where done_at is null and received_at < now() - make_interval(secs => :age) "
                    "group by peer_id"
                ),
                {"age": STALE_SECONDS},
            )
        ).all()
    for peer_id, marker in peers:
        logger.warning("peer_id=%s: сообщения висят без ответа, запускаем ход", peer_id)
        await request_flush(peer_id, marker)
    return {"stale": len(peers)}
