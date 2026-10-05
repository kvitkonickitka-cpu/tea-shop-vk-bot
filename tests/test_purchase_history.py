"""Повторные касания, задача 6: «Покупки клиента» в промпте модели."""

from __future__ import annotations

from datetime import timedelta

from app.core.config import settings
from app.modules.orders import conversation, feedback, purchases
from app.modules.orders.models import OrderFeedback
from tests.test_auto_invoice import said
from tests.test_feedback_repeat_optout import NOW, PEER, make_order


async def test_block_without_personal_data(clean):
    first = await make_order(clean, delivered_days_ago=60, created_at=NOW - timedelta(days=64),
                             details={"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
                                      "recipient_email": "ivanov@mail.ru", "address": "Краснодар"})
    await feedback.rate(PEER, first.id, "great", "button")
    async with clean() as session:
        session.add(OrderFeedback(order_id=first.id, peer_id=PEER, text="Очень ароматный, буду брать ещё",
                                  publish_consent="yes"))
        await session.commit()
    second = await make_order(clean, ozon_posting="0002-1", status="refunded", delivered_days_ago=10,
                              created_at=NOW - timedelta(days=14),
                              items=[{"name": "Да Хун Пао", "quantity": 2, "price": 1500}])
    await feedback.rate(PEER, second.id, "no", "text")
    block = await purchases.context(PEER)
    assert block.splitlines()[:3] == [
        "Покупки клиента (последние сначала):",
        "- 06.10.2026 — Да Хун Пао × 2 — возвращён; оценка: не понравился",
        "- 17.08.2026 — Те Гуань Инь 100 г — вручён; оценка: понравился; "
        "отзыв: «Очень ароматный, буду брать ещё»",
    ]
    assert block.endswith(purchases.INSTRUCTION)
    for secret in ("Иванов", "+7900", "ivanov@", "Краснодар"):
        assert secret not in block


async def test_no_orders_no_block_and_flag(clean, monkeypatch):
    assert await purchases.context(PEER) == ""
    await make_order(clean)
    monkeypatch.setattr(settings, "purchase_history_in_prompt_enabled", False)
    assert await purchases.context(PEER) == ""


async def test_block_reaches_the_model(clean, monkeypatch):
    await make_order(clean, delivered_days_ago=30)
    seen = {}

    async def converse(messages, system_prompt, tools, **_):
        seen["prompt"] = system_prompt
        return said("Подберу!")

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    await conversation.handle_turn(PEER, "посоветуйте что-нибудь")
    assert "Покупки клиента (последние сначала):\n- " in seen["prompt"]
    assert "Те Гуань Инь 100 г — вручён" in seen["prompt"]
