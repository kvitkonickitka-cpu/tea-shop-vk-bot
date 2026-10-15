"""Очередь событий ВК в Postgres — замена Yandex Message Queue на ВМ Timeweb.

Зачем: ВК ждёт ответа на вебхук около восьми секунд, а ход с заказом —
три запроса к Claude и инструменты — бывает и дольше. Без очереди ВК
считал доставку неудачной, а склейка сообщений была выключена: ждать
тишины 2–5 секунд под его секундомером нельзя. С очередью вебхук кладёт
событие в таблицу и отвечает «ok» за доли секунды; разбирает его
обработчик здесь же, в процессе бота, уже без секундомера.

Договор тот же, что у Yandex-очереди и триггера:

- событие разбирает `events.process_event` — тот же код;
- упало — повтор с паузой (до _MAX_ATTEMPTS раз); удачное — `done_at`;
- взятое, но не законченное (контейнер перезапустили на середине) через
  _LEASE_SECONDS снова свободно. Повтор безопасен: дедупликация по event_id
  и отметки `inbound_messages` те же, что при повторной доставке очередью;
- разные диалоги идут параллельно (до `local_queue_concurrency`), один
  диалог — одним ходом: это держит блокировка в `inbound._run_turn`.

Процесс uvicorn на ВМ один, поэтому обработчик будится сразу из `enqueue`,
а опрос таблицы раз в секунду — только для повторов и подбора после рестарта.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120
_MAX_ATTEMPTS = 5
_POLL_SECONDS = 1.0
# Ход живёт до минуты (inbound._INVOCATION_SECONDS); событие дольше этого
# считаем зависшим и снимаем, чтобы оно не держало место обработчика.
_EVENT_TIMEOUT_SECONDS = 150
_KEEP_DONE_HOURS = 24

_wake = asyncio.Event()


def is_enabled() -> bool:
    return settings.local_queue_enabled


async def enqueue(event: dict) -> str:
    """Положить событие в очередь. Секрет сообщества сюда не попадает."""
    body = {k: v for k, v in event.items() if k != "secret"}
    async with get_session_factory()() as session:
        new_id = (await session.execute(
            text("insert into queued_events (body) values (cast(:body as jsonb)) returning id"),
            {"body": _json(body)},
        )).scalar_one()
        await session.commit()
    _wake.set()
    return str(new_id)


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def _claim() -> tuple[int, dict, int] | None:
    """Взять одно готовое событие. Двое одно и то же не возьмут: SKIP LOCKED."""
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            """
            update queued_events set taken_at = now(), attempts = attempts + 1
            where id = (
                select id from queued_events
                where done_at is null and available_at <= now()
                  and (taken_at is null or taken_at < now() - make_interval(secs => :lease))
                order by id
                for update skip locked
                limit 1
            )
            returning id, body, attempts
            """
        ), {"lease": _LEASE_SECONDS})).first()
        await session.commit()
    return (row[0], row[1], row[2]) if row else None


async def _done(event_id: int) -> None:
    async with get_session_factory()() as session:
        await session.execute(text("update queued_events set done_at = now() where id = :id"), {"id": event_id})
        await session.commit()


async def _failed(event_id: int, attempts: int, error: BaseException) -> None:
    kind = type(error).__name__
    async with get_session_factory()() as session:
        if attempts >= _MAX_ATTEMPTS:
            # Сдаёмся громко: в логе ERROR, сообщение клиента без ответа.
            # Отметки «обработано» нет — тик подберёт висящее в inbound_messages.
            await session.execute(text(
                "update queued_events set done_at = now(), last_error = :e where id = :id"
            ), {"id": event_id, "e": f"сдались после {attempts}: {kind}"})
            logger.error("Очередь: событие %s не разобралось за %s попыток (%s)", event_id, attempts, kind)
        else:
            # Пауза растёт: 10, 20, 40, 80 с.
            await session.execute(text(
                """
                update queued_events
                set taken_at = null, last_error = :e,
                    available_at = now() + make_interval(secs => :delay)
                where id = :id
                """
            ), {"id": event_id, "e": kind, "delay": 10 * 2 ** (attempts - 1)})
            logger.warning("Очередь: событие %s упало (%s), повтор #%s", event_id, kind, attempts + 1)
        await session.commit()


async def _handle(event_id: int, body: dict, attempts: int, slots: asyncio.Semaphore) -> None:
    from app.modules import events

    started = time.monotonic()
    try:
        await asyncio.wait_for(events.process_event(body), timeout=_EVENT_TIMEOUT_SECONDS)
    except Exception as error:
        logger.exception("Очередь: событие %s (%s) не разобралось", event_id, body.get("type"))
        try:
            await _failed(event_id, attempts, error)
        except Exception:
            logger.exception("Очередь: не записали сбой события %s — подберём по аренде", event_id)
    else:
        try:
            await _done(event_id)
        except Exception:
            # Не страшно: через _LEASE_SECONDS событие возьмут снова, и
            # дедупликация по event_id его пропустит.
            logger.exception("Очередь: не отметили событие %s разобранным", event_id)
        logger.info("Очередь: событие %s (%s) разобрано за %.1f с",
                    event_id, body.get("type"), time.monotonic() - started)
    finally:
        slots.release()


async def run() -> None:
    """Обработчик очереди: живёт, пока живёт приложение."""
    slots = asyncio.Semaphore(max(1, settings.local_queue_concurrency))
    tasks: set[asyncio.Task] = set()
    logger.info("Очередь: обработчик запущен, до %s событий одновременно", settings.local_queue_concurrency)
    while True:
        await slots.acquire()
        try:
            claimed = await _claim()
        except asyncio.CancelledError:
            slots.release()
            raise
        except Exception:
            # База недоступна: бот в это время работает без очереди сам
            # (вебхук упадёт на enqueue и разберёт на месте).
            slots.release()
            logger.warning("Очередь: не смогли взять событие, база недоступна?", exc_info=True)
            await asyncio.sleep(5)
            continue
        if claimed is None:
            slots.release()
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), timeout=_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        task = asyncio.create_task(_handle(*claimed, slots))
        tasks.add(task)
        task.add_done_callback(tasks.discard)


async def stats() -> dict:
    """Для пульса и ассистента: сколько ждёт и сколько ждёт самое старое."""
    async with get_session_factory()() as session:
        row = (await session.execute(text(
            """
            select count(*) filter (where done_at is null) as waiting,
                   extract(epoch from now() - min(created_at) filter (where done_at is null)) as oldest_s,
                   count(*) filter (where done_at is not null and last_error like 'сдались%'
                                    and done_at > now() - interval '24 hours') as gave_up_24h
            from queued_events
            """
        ))).first()
    return {"waiting": row[0], "oldest_seconds": int(row[1]) if row[1] is not None else None,
            "gave_up_24h": row[2]}


async def cleanup() -> int:
    """Тик: разобранное старше суток — удалить, там тексты сообщений."""
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "delete from queued_events where done_at < now() - make_interval(hours => :h)"
        ), {"h": _KEEP_DONE_HOURS})
        await session.commit()
    return result.rowcount or 0
