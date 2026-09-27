"""Задача 3: одно напоминание про брошенный черновик."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core import worktime
from app.core.config import settings
from app.messages import client as client_messages, marketing
from app.modules.catalog import service as catalog_service
from app.modules.dialog import escalation_state, history as dialog_history
from app.modules.dialog.models import Conversation, ConversationMessage
from app.modules.orders import draft_nudge, state
from app.modules.orders.state import OrderDraft

PEER = 9970
# 15:00 по Москве: внутри окна 10–21.
NOW = datetime(2026, 9, 28, 15, 0, tzinfo=worktime.MSK)

ITEMS = [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 900}]


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def fake_send(peer_id, text, random_id=None):
        messages.append((peer_id, text))

    monkeypatch.setattr(client_messages.vk_client, "send_message", fake_send)
    monkeypatch.setattr(settings, "free_delivery_threshold", "")
    monkeypatch.setattr(
        catalog_service, "load_items",
        lambda: [{"name": "Габа 50 г", "price": 450, "in_stock": True},
                 {"name": "Шу Пуэр 100 г", "price": 300, "in_stock": False}],
    )
    return messages


async def make_draft(db, *, started_hours_ago=4, bot_said_hours_ago=4, priced=False, **details):
    started = NOW - timedelta(hours=started_hours_ago)
    draft = OrderDraft(items=list(ITEMS), items_total=1800, stage="awaiting_delivery",
                       details={"started_at": started.isoformat(), **details})
    if priced:
        draft.delivery_method = "ozon_pvz"
        draft.delivery_label = "Ozon, пункт выдачи: Краснодар, Ставропольская, 230"
        draft.delivery_cost = 121
        draft.details["ozon_point_id"] = 1
        draft.stage = "awaiting_confirmation"
    await state.set_draft(PEER, draft)
    async with db() as session:
        session.add(Conversation(peer_id=PEER, message_count=2))
        await session.flush()
        session.add(ConversationMessage(peer_id=PEER, role="user", content="хочу улун",
                                        created_at=NOW - timedelta(hours=bot_said_hours_ago, minutes=1)))
        session.add(ConversationMessage(peer_id=PEER, role="assistant", content="Записала.",
                                        author=dialog_history.AUTHOR_BOT,
                                        created_at=NOW - timedelta(hours=bot_said_hours_ago)))
        await session.commit()


async def test_unpriced_draft_gets_one_nudge(clean, sent):
    await make_draft(clean)
    result = await draft_nudge.check_drafts(NOW)
    assert result["sent"] == 1
    assert sent == [(PEER, (
        "Вы выбирали Те Гуань Инь 100 г × 2 — 1800 ₽. Посчитать доставку? Назовите город — "
        "скажу цену и ближайшие пункты выдачи.\n"
        "Передумали — просто не отвечайте, больше напоминать не буду 🙂"
    ))]
    # Никогда больше — даже когда тишина продолжается.
    assert (await draft_nudge.check_drafts(NOW + timedelta(hours=5)))["sent"] == 0
    assert len(sent) == 1
    # Событие записано в журнал отправок, а текст — в историю диалога.
    history = await dialog_history.get_history(PEER)
    assert history[-1]["content"].startswith("Вы выбирали")


async def test_priced_draft_names_total_and_small_gap(clean, sent, monkeypatch):
    monkeypatch.setattr(settings, "free_delivery_threshold", "2000")
    await make_draft(clean, priced=True)
    await draft_nudge.check_drafts(NOW)
    assert sent[0][1] == (
        "Заказ ждёт вас: Те Гуань Инь 100 г × 2 и доставка в пункт выдачи Ozon "
        "(Краснодар, Ставропольская, 230) — итого 1921 ₽.\n"
        "До бесплатной доставки не хватает 200 ₽ — можно добавить ещё пачку.\n"
        "Оформить? Если нужно что-то поменять — напишите, поправлю. "
        "Передумали — просто не отвечайте, больше напоминать не буду 🙂"
    )


async def test_gap_bigger_than_a_pack_is_not_mentioned(clean, sent, monkeypatch):
    # Самая дешёвая пачка в наличии — 450 ₽, не хватает 700.
    monkeypatch.setattr(settings, "free_delivery_threshold", "2500")
    await make_draft(clean, priced=True)
    await draft_nudge.check_drafts(NOW)
    assert "До бесплатной доставки" not in sent[0][1]


async def test_too_early_after_last_reply(clean, sent):
    await make_draft(clean, bot_said_hours_ago=2)
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_client_wrote_last(clean, sent):
    await make_draft(clean)
    async with clean() as session:
        session.add(ConversationMessage(peer_id=PEER, role="user", content="а?",
                                        created_at=NOW - timedelta(hours=3, minutes=30)))
        await session.commit()
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_old_draft_is_left_alone(clean, sent):
    await make_draft(clean, started_hours_ago=49, bot_said_hours_ago=40)
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_invoiced_draft_is_left_alone(clean, sent):
    await make_draft(clean, order_id=17)
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_open_escalation_blocks(clean, sent):
    await make_draft(clean)
    await escalation_state.mark_open(PEER)
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_manager_wrote_after_draft(clean, sent):
    await make_draft(clean)
    async with clean() as session:
        session.add(ConversationMessage(peer_id=PEER, role="assistant", content="Здравствуйте!",
                                        author=dialog_history.AUTHOR_MANAGER,
                                        created_at=NOW - timedelta(hours=3, minutes=30)))
        await session.commit()
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_opted_out_client(clean, sent):
    await make_draft(clean)
    await marketing.opt_out(PEER)
    assert (await draft_nudge.check_drafts(NOW))["sent"] == 0


async def test_evening_waits_for_morning(clean, sent):
    await make_draft(clean)
    evening = NOW.replace(hour=21, minute=30)
    assert (await draft_nudge.check_drafts(evening))["sent"] == 0
    night = NOW.replace(hour=3)
    assert (await draft_nudge.check_drafts(night))["sent"] == 0
    early = (NOW + timedelta(days=1)).replace(hour=9, minute=30)
    assert (await draft_nudge.check_drafts(early))["sent"] == 0
    morning = (NOW + timedelta(days=1)).replace(hour=10, minute=0)
    assert (await draft_nudge.check_drafts(morning))["sent"] == 1


def test_started_at_is_kept_across_edits():
    draft = OrderDraft(items=list(ITEMS), items_total=1800, details={"started_at": "x"})
    draft.details.setdefault("started_at", "y")
    assert draft.details["started_at"] == "x"


async def test_set_draft_stamps_start(clean):
    await state.set_draft(PEER, OrderDraft(items=list(ITEMS), items_total=1800,
                                           stage="awaiting_delivery"))
    first = (await state.get_draft(PEER)).details["started_at"]
    draft = await state.get_draft(PEER)
    draft.delivery_method = "cdek_pvz"
    await state.set_draft(PEER, draft)
    assert (await state.get_draft(PEER)).details["started_at"] == first
