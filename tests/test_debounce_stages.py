"""Funnel v3, задача 4: пауза склейки зависит от этапа."""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.messages import templates
from app.modules.dialog import history as dialog_history, inbound
from app.modules.orders import state
from app.modules.orders.state import OrderDraft

PEER = 9973


@pytest.fixture
def pauses(monkeypatch):
    monkeypatch.setattr(settings, "message_debounce_seconds_default", 0.3)
    monkeypatch.setattr(settings, "message_debounce_seconds_collecting", 1.2)
    monkeypatch.setattr(settings, "message_debounce_max_seconds", 5)


async def collecting_stage(bot_said: str):
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    await dialog_history.append_exchange(PEER, "беру те гуань инь", bot_said)


async def waited(text: str, n: int) -> float:
    await inbound._store(f"st{n}", {"peer_id": PEER, "text": text})
    started = time.monotonic()
    await inbound._wait_for_quiet(PEER)
    return time.monotonic() - started


async def test_consultation_waits_short(clean, pauses):
    assert await inbound.pause_for(PEER) == 0.3
    assert await waited("а какой улун посоветуете?", 1) < 0.8


@pytest.mark.parametrize("bot_said", [
    "Записала. " + templates.ASK_WHERE,
    "Пункты: 1) … 2) … " + templates.POINTS_HINT,
    templates.ASK_RECIPIENT,
    "Выберите пункт и одним сообщением пришлите ФИО, телефон и почту — сразу пришлю счёт.",
])
async def test_collecting_waits_longer(clean, pauses, bot_said):
    await collecting_stage(bot_said)
    await inbound._store("st0", {"peer_id": PEER, "text": "Иванов Иван"})
    assert await inbound.pause_for(PEER) == 1.2


async def test_collecting_wait_is_real(clean, pauses):
    await collecting_stage(templates.ASK_RECIPIENT)
    assert await waited("Иванов Иван", 1) >= 1.1


async def test_phone_and_email_end_the_wait_early(clean, pauses):
    await collecting_stage(templates.ASK_RECIPIENT)
    took = await waited("Иванов Иван, +7 900 123-45-67, ivanov@mail.ru", 1)
    assert took < 0.8


async def test_contacts_in_pieces_switch_to_short_pause(clean, pauses):
    await collecting_stage(templates.ASK_RECIPIENT)
    await inbound._store("st1", {"peer_id": PEER, "text": "Иванов Иван"})
    await inbound._store("st2", {"peer_id": PEER, "text": "89001234567"})
    assert await inbound.pause_for(PEER) == 1.2
    await inbound._store("st3", {"peer_id": PEER, "text": "ivanov@mail.ru"})
    assert await inbound.pause_for(PEER) == 0.3


async def test_no_draft_means_consultation_even_after_a_question(clean, pauses):
    await dialog_history.append_exchange(PEER, "привет", templates.ASK_WHERE)
    assert await inbound.pause_for(PEER) == 0.3


def test_contacts_detection():
    assert inbound.has_contacts("89001234567 a@b.ru")
    assert not inbound.has_contacts("89001234567")
    assert not inbound.has_contacts("ivanov@mail.ru")
