"""Задача 5: отзывы, «повторить заказ?» и отписка."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import worktime
from app.core.config import settings
from app.messages import client as client_messages, manager as manager_messages, marketing, templates
from app.messages.models import ManagerNotification
from app.modules.dialog import escalation_state, history as dialog_history
from app.modules.orders import (
    conversation, delivery_events, feedback, repeat_nudge, repository as orders_repository, state,
)
from app.modules.orders.models import Order, OrderFeedback
from app.modules.orders.state import OrderDraft

PEER = 9980
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=worktime.MSK)
TEA = [{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}]


async def make_order(db, *, delivered_days_ago: float = 3, peer_id: int = PEER, items=None, **fields):
    delivered = NOW - timedelta(days=delivered_days_ago)
    values = dict(
        peer_id=peer_id, items=items or TEA, items_total=900, delivery_cost=121, total=1021,
        delivery_method="ozon_pvz", status="confirmed", payment_status=orders_repository.PAID,
        ozon_posting="0001-1", details={}, created_at=delivered - timedelta(days=4),
        handed_over_at=delivered - timedelta(days=3), delivered_at=delivered,
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        return order


@pytest.fixture
def outbox(monkeypatch):
    client, manager = [], []

    async def to_client(peer_id, text, random_id=None):
        client.append(text)

    async def to_manager(text, chat_id=None):
        manager.append(text)

    monkeypatch.setattr(client_messages.vk_client, "send_message", to_client)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    return SimpleNamespace(client=client, manager=manager)


# --- отзывы ---------------------------------------------------------------

async def test_feedback_saved_and_updated_without_duplicate_card(clean, outbox):
    order = await make_order(clean)
    result = await feedback.save(
        PEER, {"order_id": order.id, "text": "Чай отличный, <b>спасибо</b>", "publish_consent": "unknown"},
        now=NOW,
    )
    assert "спроси, можно ли опубликовать" in result
    assert outbox.manager == [
        f"⭐ <b>Отзыв по заказу №{order.id}</b>\nЧай отличный, &lt;b&gt;спасибо&lt;/b&gt;\n\n"
        f"Публикация: не спрашивали\nhttps://vk.com/gim240363526?sel={PEER}"
    ]

    # Уточнил текст, согласие то же — карточка не повторяется.
    await feedback.save(PEER, {"order_id": order.id, "text": "Чай отличный, и упаковка",
                               "publish_consent": "unknown"}, now=NOW)
    assert len(outbox.manager) == 1
    # Разрешил публикацию — новая карточка.
    await feedback.save(PEER, {"order_id": order.id, "text": "Чай отличный, и упаковка",
                               "publish_consent": "yes"}, now=NOW)
    assert len(outbox.manager) == 2 and "Публикация: можно" in outbox.manager[-1]

    async with clean() as session:
        rows = (await session.execute(select(OrderFeedback))).scalars().all()
        queued = (await session.execute(
            select(ManagerNotification).where(ManagerNotification.kind == manager_messages.FEEDBACK)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].publish_consent == "yes"
    assert rows[0].text == "Чай отличный, и упаковка"
    assert len(queued) == 2  # через очередь, а не напрямую


async def test_feedback_only_for_recent_delivery(clean, outbox):
    old = await make_order(clean, delivered_days_ago=15)
    assert await feedback.recent_delivered(PEER, NOW) is None
    result = await feedback.save(PEER, {"order_id": old.id, "text": "ок", "publish_consent": "no"}, now=NOW)
    assert "не записан" in result and outbox.manager == []


async def test_feedback_tool_and_prompt_only_after_delivery(clean, outbox, monkeypatch):
    seen = []

    async def converse(messages, system_prompt, tools, **_):
        seen.append((system_prompt, [t["name"] for t in tools]))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Спасибо!")])

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(feedback, "FEEDBACK_WINDOW", timedelta(days=14))
    await conversation.handle_turn(PEER, "привет")
    assert "save_feedback" not in seen[-1][1]

    await make_order(clean, delivered_at=datetime.now(timezone.utc) - timedelta(days=2))
    await conversation.handle_turn(PEER, "чай огонь")
    prompt, tools = seen[-1]
    assert "save_feedback" in tools
    assert "У клиента недавно вручён заказ — Те Гуань Инь 100 г." in prompt


# --- повторить заказ ------------------------------------------------------

def test_repeat_delay_by_packs(monkeypatch):
    monkeypatch.setattr(settings, "repeat_nudge_days_per_pack", 21)
    monkeypatch.setattr(settings, "repeat_nudge_max_days", 60)
    one = SimpleNamespace(items=[{"quantity": 1}])
    two = SimpleNamespace(items=[{"quantity": 1}, {"quantity": 1}])
    five = SimpleNamespace(items=[{"quantity": 5}])
    assert repeat_nudge.delay_for(one) == timedelta(days=21)
    assert repeat_nudge.delay_for(two) == timedelta(days=42)
    assert repeat_nudge.delay_for(five) == timedelta(days=60)


async def test_repeat_nudge_once(clean, outbox):
    await make_order(clean, delivered_days_ago=22)
    # Ещё рано: две пачки — 42 дня.
    await make_order(clean, peer_id=PEER + 1, delivered_days_ago=22, ozon_posting="0002-1",
                     items=[{"name": "Да Хун Пао 100 г", "quantity": 2, "price": 1200}])
    assert (await repeat_nudge.check(NOW))["sent"] == 1
    assert outbox.client == [
        "Здравствуйте! Около 3 недель назад вы получили Те Гуань Инь 100 г — чай, наверное, "
        "подходит к концу 🍵\n"
        "Повторить заказ с доставкой туда же? Или подскажу, что попробовать ещё.\n"
        "Если не хотите таких напоминаний — напишите «стоп»."
    ]
    assert (await repeat_nudge.check(NOW + timedelta(hours=2)))["sent"] == 0
    # Напоминание в истории: «да» модель поймёт как ответ на него.
    history = await dialog_history.get_history(PEER)
    assert history[-1]["content"].startswith("Здравствуйте! Около 3 недель")


async def test_repeat_nudge_not_after_new_order(clean, outbox):
    first = await make_order(clean, delivered_days_ago=22)
    await make_order(clean, delivered_days_ago=1, ozon_posting="0003-1",
                     created_at=first.delivered_at + timedelta(days=5))
    assert (await repeat_nudge.check(NOW))["sent"] == 0


@pytest.mark.parametrize("case", ["refunded", "canceled", "opted_out", "draft", "escalation", "night"])
async def test_repeat_nudge_blockers(clean, outbox, case):
    fields = {"status": case} if case in ("refunded", "canceled") else {}
    await make_order(clean, delivered_days_ago=22, **fields)
    now = NOW
    if case == "opted_out":
        await marketing.opt_out(PEER)
    if case == "draft":
        await state.set_draft(PEER, OrderDraft(items=TEA, items_total=900, stage="awaiting_delivery"))
    if case == "escalation":
        await escalation_state.mark_open(PEER)
    if case == "night":
        now = NOW.replace(hour=21, minute=15)
    assert (await repeat_nudge.check(now))["sent"] == 0


# --- отписка --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "стоп", "Стоп!", "хватит", "Хватит уже", "не пишите", "Не пишите мне больше",
    "отпишите", "отпишите меня пожалуйста", "Отписаться", "стоп, спасибо",
])
def test_stop_words_recognized(text):
    assert marketing.is_stop_request(text)


@pytest.mark.parametrize("text", [
    "хватит ли 50 грамм?", "Стоп, давайте через Ozon", "стоп заказ", "а почему не пишите?",
    "чай хватит на месяц", "", "пуэр",
])
def test_stop_words_not_overreaching(text):
    assert not marketing.is_stop_request(text)


async def test_stop_sets_flag_without_model(clean, outbox, monkeypatch):
    async def converse(*args, **kwargs):
        raise AssertionError("модель не должна обрабатывать «стоп»")

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    # «Стоп» — отписка только в ответ на продающее напоминание.
    await client_messages.send(
        peer_id=PEER, ref="order:1", event_type=templates.REPEAT_NUDGE, text="Повторить заказ?"
    )
    reply = await conversation.handle_turn(PEER, "Стоп")
    assert reply == (
        "Хорошо, больше не буду присылать напоминания. Сообщения по вашим заказам и "
        "доставке будут приходить как обычно."
    )
    assert await marketing.is_opted_out(PEER)
    history = await dialog_history.get_history(PEER)
    assert [m["content"] for m in history[-2:]] == ["Стоп", reply]


async def test_opt_out_keeps_order_messages(clean, outbox, monkeypatch):
    await marketing.opt_out(PEER)
    order = await make_order(clean, delivered_at=None, handed_over_at=None)
    monkeypatch.setattr(delivery_events.order_chat, "send", lambda *a, **k: _none())
    assert await delivery_events.record(order.id, delivery_events.AT_PICKUP, source="тест")
    assert any("ждёт вас в пункте выдачи Ozon" in text for text in outbox.client)
    # Сам начал новый заказ — флаг остаётся.
    await state.set_draft(PEER, OrderDraft(items=TEA, items_total=900, stage="awaiting_delivery"))
    assert await marketing.is_opted_out(PEER)


async def _none():
    return True
