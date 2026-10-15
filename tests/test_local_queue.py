"""Своя очередь событий ВК в Postgres (app/modules/queue/local.py).

Вебхук кладёт событие и сразу отвечает ВК; обработчик разбирает его тем же
`events.process_event`. Здесь — что секрет сообщества в таблицу не попадает,
что упавшее событие повторяется с паузой и в конце бросается, что взятое и
брошенное умершим обработчиком подбирается снова, и что вебхук с очередью
не ждёт хода.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules import events
from app.modules.queue import client as queue_client, local


@pytest.fixture
def queue(db, monkeypatch):
    monkeypatch.setattr(settings, "local_queue_enabled", True)
    monkeypatch.setattr(settings, "ymq_queue_url", "")
    return db


async def _rows():
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "select id, body, attempts, taken_at, done_at, last_error, available_at > now() as later "
            "from queued_events order by id"
        ))
        return [dict(r._mapping) for r in result]


async def _clear():
    async with get_session_factory()() as session:
        await session.execute(text("delete from queued_events"))
        await session.commit()


async def test_queue_is_configured_by_the_local_flag(queue):
    assert queue_client.is_configured() and queue_client.uses_local()


async def test_enqueue_drops_the_community_secret(queue):
    await _clear()
    await queue_client.enqueue({"type": "message_new", "event_id": "e1", "secret": "s3cr3t", "object": {}})
    rows = await _rows()
    assert len(rows) == 1
    assert "secret" not in rows[0]["body"] and rows[0]["body"]["event_id"] == "e1"


async def test_success_marks_done(queue, monkeypatch):
    await _clear()
    seen = []

    async def process(body):
        seen.append(body["event_id"])

    monkeypatch.setattr(events, "process_event", process)
    await local.enqueue({"type": "message_new", "event_id": "ok-1"})
    claimed = await local._claim()
    await local._handle(*claimed, asyncio.Semaphore(0))
    rows = await _rows()
    assert seen == ["ok-1"] and rows[0]["done_at"] is not None and rows[0]["attempts"] == 1


async def test_failure_retries_with_a_pause_then_gives_up(queue, monkeypatch):
    await _clear()

    async def boom(body):
        raise RuntimeError("Иванов +79001234567")  # текст ошибки в таблицу не идёт

    monkeypatch.setattr(events, "process_event", boom)
    await local.enqueue({"type": "message_new", "event_id": "bad-1"})
    claimed = await local._claim()
    await local._handle(*claimed, asyncio.Semaphore(0))
    row = (await _rows())[0]
    assert row["done_at"] is None and row["taken_at"] is None and row["later"]
    assert row["last_error"] == "RuntimeError"
    # Пауза держит событие: сразу его не взять.
    assert await local._claim() is None

    async with get_session_factory()() as session:
        await session.execute(text("update queued_events set available_at = now(), attempts = :n"),
                              {"n": local._MAX_ATTEMPTS - 1})
        await session.commit()
    claimed = await local._claim()
    await local._handle(*claimed, asyncio.Semaphore(0))
    row = (await _rows())[0]
    assert row["done_at"] is not None and row["last_error"].startswith("сдались")
    assert "Иванов" not in row["last_error"]
    assert (await local.stats())["gave_up_24h"] == 1


async def test_abandoned_event_is_taken_again_after_the_lease(queue):
    await _clear()
    await local.enqueue({"type": "message_new", "event_id": "lost-1"})
    assert await local._claim() is not None
    assert await local._claim() is None  # взято и в работе
    async with get_session_factory()() as session:
        await session.execute(text("update queued_events set taken_at = now() - interval '10 minutes'"))
        await session.commit()
    again = await local._claim()
    assert again is not None and again[2] == 2


async def test_worker_runs_events_in_parallel(queue, monkeypatch):
    await _clear()
    monkeypatch.setattr(settings, "local_queue_concurrency", 4)
    finished = []

    async def slow(body):
        await asyncio.sleep(0.5)
        finished.append(body["event_id"])

    monkeypatch.setattr(events, "process_event", slow)
    for n in range(4):
        await local.enqueue({"type": "message_new", "event_id": f"p-{n}"})
    worker = asyncio.create_task(local.run())
    started = time.monotonic()
    while len(finished) < 4 and time.monotonic() - started < 5:
        await asyncio.sleep(0.05)
    worker.cancel()
    assert sorted(finished) == [f"p-{n}" for n in range(4)]
    # Четыре по полсекунды параллельно — заметно меньше двух секунд подряд.
    assert time.monotonic() - started < 1.6
    await asyncio.sleep(0.1)
    assert all(r["done_at"] is not None for r in await _rows())


async def test_webhook_answers_without_waiting_for_the_turn(queue, monkeypatch):
    from app.main import app

    await _clear()
    monkeypatch.setattr(settings, "vk_secret_key", "test-secret")
    called = []

    async def never(body):
        called.append(body)

    monkeypatch.setattr(events, "process_event", never)
    body = {"type": "message_new", "event_id": "hook-1", "secret": "test-secret",
            "object": {"message": {"peer_id": 1, "from_id": 1, "text": "привет"}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bot") as client:
        response = await client.post("/vk/callback", json=body)
    assert response.status_code == 200 and response.text == "ok"
    assert called == []  # разбирает обработчик очереди, не вебхук
    rows = await _rows()
    assert [r["body"]["event_id"] for r in rows] == ["hook-1"] and "secret" not in rows[0]["body"]
