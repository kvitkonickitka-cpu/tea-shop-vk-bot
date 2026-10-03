"""Задача 1: сообщения подряд — один ход; во время хода — следующий ход."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.dialog import inbound, service
from app.modules.dialog.models import InboundMessage, ProcessedEvent

PEER = 9971


class Calls(list):
    box: dict


@pytest.fixture
def turns(monkeypatch):
    calls = Calls()
    box = {"delay": 0.0, "fail": False}

    async def respond(peer_id, text, attached, *, budget_seconds=None):
        calls.append(text)
        if box["delay"]:
            await asyncio.sleep(box["delay"])
        if box["fail"]:
            raise RuntimeError("ход упал")

    monkeypatch.setattr(service, "respond", respond)
    monkeypatch.setattr(inbound, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "message_debounce_seconds_default", 0.6)
    monkeypatch.setattr(settings, "message_debounce_seconds_collecting", 0.6)
    monkeypatch.setattr(settings, "message_debounce_max_seconds", 3)
    # Без очереди остаток разбирается сразу здесь же.
    monkeypatch.setattr("app.modules.queue.client.is_configured", lambda: False)
    calls.box = box
    return calls


def msg(text: str, **extra) -> dict:
    return {"peer_id": PEER, "text": text, **extra}


async def processed(db) -> set[str]:
    async with db() as session:
        return set((await session.execute(select(ProcessedEvent.event_id))).scalars().all())


async def test_three_messages_make_one_turn(clean, turns):
    async def later(delay, event_id, text):
        await asyncio.sleep(delay)
        await inbound.accept(event_id, msg(text))

    await asyncio.gather(
        later(0.0, "e1", "Иванов Иван"),
        later(0.3, "e2", "89001234567"),
        later(0.6, "e3", "ivanov@mail.ru"),
    )
    assert turns == ["Иванов Иван\n89001234567\nivanov@mail.ru"]
    assert {"e1", "e2", "e3"} <= await processed(clean)


async def test_message_during_turn_goes_to_next_turn(clean, turns):
    turns.box["delay"] = 1.0

    async def late():
        await asyncio.sleep(0.9)  # первый ход уже идёт
        await inbound.accept("e2", msg("и ещё вопрос"))

    await asyncio.gather(inbound.accept("e1", msg("хочу улун")), late())
    assert turns == ["хочу улун", "и ещё вопрос"]
    assert {"e1", "e2"} <= await processed(clean)


async def test_button_does_not_wait(clean, turns):
    loop = asyncio.get_running_loop()
    started = loop.time()
    await inbound.accept("b1", msg("Оплатить", payload='{"a":"pay"}'))
    assert loop.time() - started < 0.5
    assert turns == ["Оплатить"]


async def test_failed_turn_leaves_messages_for_retry(clean, turns):
    turns.box["fail"] = True
    with pytest.raises(RuntimeError):
        await inbound.accept("e1", msg("хочу улун"))
    assert "e1" not in await processed(clean)
    async with clean() as session:
        row = (await session.execute(select(InboundMessage))).scalar_one()
    assert row.done_at is None

    # Очередь приносит то же событие снова — повтор отвечает.
    turns.box["fail"] = False
    await inbound.accept("e1", msg("хочу улун"))
    assert turns == ["хочу улун", "хочу улун"]
    assert "e1" in await processed(clean)


async def test_stale_messages_are_rescued(clean, turns, monkeypatch):
    monkeypatch.setattr(inbound, "STALE_SECONDS", 0)
    await inbound._store("e9", msg("зависшее"))
    await asyncio.sleep(0.05)
    assert (await inbound.rescue_stale())["stale"] == 1
    assert turns == ["зависшее"]
