"""Задача 6: «Повторить заказ» одним нажатием."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core import worktime
from app.messages import keyboard as keyboards
from app.messages.models import FunnelEvent
from app.modules.catalog import service as catalog_service
from app.modules.orders import repeat_nudge, repository as orders_repository, state
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft
from tests.test_auto_invoice import said, tool_use
from tests.test_returning_client import POINT, past_order
from tests.test_vk_buttons import FULL, PEER, say, world  # noqa: F401


async def the_order(db) -> Order:
    await past_order(db)
    async with db() as session:
        order = (await session.execute(select(Order))).scalar_one()
        order.delivered_at = datetime.now(timezone.utc) - timedelta(days=22)
        await session.commit()
        return order


async def nudge(db, world) -> dict:
    order = await the_order(db)
    await keyboards.remember_client(PEER, FULL)
    # Вручено 22 дня назад, пачка одна — срок вышел вчера; сегодня в 15:00 — окно.
    moment = datetime.now(worktime.MSK).replace(hour=15, minute=0)
    assert (await repeat_nudge.check(moment))["sent"] == 1
    text, board = world["sent"][-1]
    assert text.startswith("Здравствуйте! Около 3 недель назад вы получили Те Гуань Инь (тест)")
    return {b["action"]["label"]: b["action"] for b in board["buttons"][0]}


async def test_repeat_button_sends_invoice_with_current_prices(clean, world, monkeypatch):
    buttons = await nudge(clean, world)
    assert list(buttons) == ["Повторить", "Выбрать другое"]
    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Те Гуань Инь (тест)", "price": 1600, "in_stock": True, "recommended": []}])
    await say("Повторить", 5, json.loads(buttons["Повторить"]["payload"]))
    text, board = world["sent"][-1]
    assert world["model"] == [] and world["payments"] == [1]
    assert "• Те Гуань Инь (тест) × 1 — 1600 ₽" in text and f"Доставка: пункт выдачи Ozon, {POINT} — 121 ₽" in text
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in text and "Итого: 1721 ₽" in text
    assert board["buttons"][0][0]["action"]["label"] == "Оплатить 1721 ₽"
    async with clean() as session:
        events = (await session.execute(select(FunnelEvent.event).order_by(FunnelEvent.id))).scalars().all()
    assert events == ["touch:repeat_nudge", "button:repeat", "invoice_repeat"]


async def test_missing_item_hands_over_to_model(clean, world, monkeypatch):
    buttons = await nudge(clean, world)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": False, "recommended": []}])
    world["script"] = [said("Этого чая сейчас нет, могу предложить…")]
    await say("Повторить", 5, json.loads(buttons["Повторить"]["payload"]))
    assert world["payments"] == [] and world["model"] == ["Повторить"]


async def test_unavailable_point_is_explained(clean, world, monkeypatch):
    from app.modules.orders import conversation

    buttons = await nudge(clean, world)

    async def refuse(draft, point_id):
        raise RuntimeError("closed")

    prompts = []
    real = conversation.claude_client.converse

    async def spy(messages, system_prompt, tools):
        prompts.append(system_prompt)
        return await real(messages, system_prompt, tools)

    monkeypatch.setattr(conversation, "_ozon_price", refuse)
    monkeypatch.setattr(conversation.claude_client, "converse", spy)
    world["script"] = [said("Прошлый пункт закрылся, куда везти?")]
    await say("Повторить", 5, json.loads(buttons["Повторить"]["payload"]))
    assert world["payments"] == []
    assert "не принимает посылки" in prompts[0] and "Счёт не выставлен" in prompts[0]
    draft = await state.get_draft(PEER)
    assert [i["name"] for i in draft.items] == ["Те Гуань Инь (тест)"] and "repeat_note" not in draft.details


async def test_text_yes_uses_the_tool(clean, world):
    order = await the_order(clean)
    world["script"] = [tool_use("repeat_order", order_id=order.id)]
    await say("да, давайте так же", 1)
    assert world["payments"] == [1] and f"Заказ №" in world["sent"][-1][0]


async def test_choose_other_goes_to_model(clean, world):
    buttons = await nudge(clean, world)
    world["script"] = [said("Что вам хочется попробовать?")]
    await say("Выбрать другое", 5, json.loads(buttons["Выбрать другое"]["payload"]))
    assert world["model"] == ["Выбрать другое"] and world["payments"] == []


async def test_repeat_is_stale_when_another_order_is_in_progress(clean, world):
    buttons = await nudge(clean, world)
    await state.set_draft(PEER, OrderDraft(items=[{"name": "Да Хун Пао", "quantity": 1, "price": 1500}],
                                           items_total=1500, stage="awaiting_delivery"))
    await say("Повторить", 5, json.loads(buttons["Повторить"]["payload"]))
    assert "Эта кнопка уже неактуальна." in [t for t, _ in world["sent"]]
    assert world["payments"] == []


async def test_foreign_order_is_refused(clean, world):
    order = await the_order(clean)
    async with clean() as session:
        row = await session.get(Order, order.id)
        row.peer_id = PEER + 1
        await session.commit()
    await say("Повторить", 5, {"a": "repeat", "o": order.id})
    assert world["sent"][0][0] == "Эта кнопка уже неактуальна." and world["payments"] == []
