"""Повторные касания, задача 2: «Как вам чай?» кнопками через три дня после вручения."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import templates
from app.messages.models import ManagerNotification
from app.modules.orders import buttons, conversation, feedback, retention
from app.modules.orders.models import Order, OrderFeedback, OrderRating
from tests.test_feedback_repeat_optout import NOW, PEER, make_order  # noqa: F401


@pytest.fixture
def outbox(monkeypatch):
    from types import SimpleNamespace

    from app.messages import client as client_messages, manager as manager_messages

    box = SimpleNamespace(client=[], boards=[], manager=[])

    async def to_client(peer_id, text, random_id=None, keyboard=None):
        box.client.append(text)
        box.boards.append(json.loads(keyboard) if keyboard else None)

    async def to_manager(text, chat_id=None):
        box.manager.append(text)

    async def full_buttons(peer_id):
        return {"inline_keyboard": True, "button_actions": ["text", "open_link"]}

    monkeypatch.setattr(client_messages.vk_client, "send_message", to_client)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    monkeypatch.setattr("app.messages.keyboard.client_info_of", full_buttons)
    return box


def labels(board):
    return [b["action"]["label"] for row in board["buttons"] for b in row]


async def press(order_id: int, rating: str):
    payload = json.dumps({"a": "rate", "o": order_id, "r": rating, "t": "feedback_ask"})
    return await buttons.handle(PEER, {"payload": payload})


async def test_ask_three_days_after_delivery(clean, outbox):
    order = await make_order(clean, delivered_days_ago=3)
    assert (await retention.check(NOW - timedelta(hours=1)))["sent"] == 0  # ещё рано
    assert (await retention.check(NOW))["sent"] == 1
    assert outbox.client == ["Здравствуйте! Как вам Те Гуань Инь 100 г? 🍵"]
    assert labels(outbox.boards[0]) == ["Очень понравился", "Нормально", "Не моё"]
    payload = json.loads(outbox.boards[0]["buttons"][0][0]["action"]["payload"])
    assert payload == {"a": "rate", "o": order.id, "r": "great", "t": "feedback_ask"}
    assert (await retention.check(NOW + timedelta(days=1)))["sent"] == 0  # один раз


async def test_several_teas_ask_by_order_number(clean, outbox):
    order = await make_order(clean, delivered_days_ago=3, items=[
        {"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900},
        {"name": "Да Хун Пао", "quantity": 1, "price": 1500},
    ])
    await retention.check(NOW)
    assert outbox.client == [f"Здравствуйте! Как вам чай из заказа №{order.id}? 🍵"]


async def test_no_ask_after_shelf_or_with_review(clean, outbox):
    await make_order(clean, delivered_days_ago=10.5)  # 3 дня + 7 дней годности прошли
    reviewed = await make_order(clean, delivered_days_ago=4, ozon_posting="0002-1")
    async with clean() as session:
        session.add(OrderFeedback(order_id=reviewed.id, peer_id=PEER, text="Отличный", publish_consent="yes"))
        await session.commit()
    assert (await retention.check(NOW))["sent"] == 0


async def test_rating_buttons(clean, outbox):
    order = await make_order(clean, delivered_days_ago=3)
    press_great = await press(order.id, "great")
    assert press_great.reply == templates.rated_great()
    async with clean() as session:
        row = await session.get(OrderRating, order.id)
    assert (row.rating, row.source) == ("great", "button")
    await press(order.id, "great")  # перенажатие той же — карточки второй нет
    assert (await press(order.id, "ok")).reply == templates.rated_ok()
    no = await press(order.id, "no")
    assert no.reply == templates.rated_no() and not no.to_model
    async with clean() as session:
        cards = (await session.execute(select(ManagerNotification.payload))).scalars().all()
    assert len(cards) == 2
    assert cards[0].startswith(f"⭐ <b>Оценка заказа №{order.id}: «Очень понравился»</b>")
    assert cards[1].startswith(f"🤔 <b>Оценка заказа №{order.id}: «Не моё»</b>")
    # Модель на следующем ходу знает оценку и что делать.
    assert "кнопкой «Не моё»" in feedback.prompt_for(order, await feedback.rating_of(order.id))


async def test_rating_for_someone_elses_order_is_stale(clean, outbox):
    order = await make_order(clean, peer_id=PEER + 7, delivered_days_ago=3)
    assert (await press(order.id, "great")).reply == templates.button_stale()


async def test_text_review_carries_rating(clean, outbox):
    order = await make_order(clean, delivered_days_ago=3)
    await feedback.save(PEER, {"order_id": order.id, "text": "Не моё, горчит", "publish_consent": "no",
                               "rating": "no"}, NOW)
    async with clean() as session:
        row = await session.get(OrderRating, order.id)
    assert (row.rating, row.source) == ("no", "text")


async def test_complaint_marks_order_and_stops_touches(clean, outbox, monkeypatch):
    order = await make_order(clean, delivered_days_ago=3)
    monkeypatch.setattr(conversation, "_notify_manager", lambda *a, **k: _true())
    await conversation._execute_escalate_to_manager(
        PEER, {"question": "в пачке посторонний запах", "reason": "жалоба", "complaint": True}
    )
    async with clean() as session:
        assert (await session.get(Order, order.id)).details.get("complaint_at")
    from app.modules.dialog import escalation_state

    await escalation_state.mark_resolved(PEER)
    assert "жалоба" in await retention.blocker(PEER, NOW + timedelta(days=30))


async def _true():
    return True


async def test_feedback_ask_goes_before_repeat_and_repeat_waits(clean, outbox, monkeypatch):
    monkeypatch.setattr(settings, "marketing_min_gap_days", 3)
    old = await make_order(clean, delivered_days_ago=22)
    await make_order(clean, delivered_days_ago=3, ozon_posting="0002-1",
                     created_at=old.delivered_at + timedelta(days=1))
    # У старого заказа — новый после, «Повторить» по нему не созреет; добавим
    # клиенту другого: созрели оба касания у одного человека.
    peer2 = PEER + 1
    await make_order(clean, peer_id=peer2, delivered_days_ago=22, ozon_posting="0003-1")
    second = await make_order(clean, peer_id=peer2, delivered_days_ago=3, ozon_posting="0004-1",
                              created_at=NOW - timedelta(days=22, hours=1))
    result = await retention.check(NOW, peer_id=peer2)
    sent = [d for d in result["decisions"] if d.outcome == "sent"]
    assert [(d.kind, d.order_id) for d in sent] == [(templates.FEEDBACK_ASK, second.id)]
    # «Повторить» ждёт паузу: через день — ещё нет.
    assert (await retention.check(NOW + timedelta(days=1), peer_id=peer2))["sent"] == 0
