"""Задача 5: постоянный клиент — один вопрос вместо трёх (флаг мгновенного счёта выключен)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import templates
from app.messages.models import FunnelEvent
from app.modules.orders import conversation, repository as orders_repository, state
from app.modules.orders.models import Order
from tests.test_auto_invoice import OWN_EVENTS, said, tool_use
from tests.test_vk_buttons import FULL, PEER, say, world  # noqa: F401

POINT = "Краснодар, Ставропольская улица, 230"


async def past_order(db):
    async with db() as session:
        session.add(Order(
            peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}],
            items_total=1500, delivery_cost=121, total=1621, delivery_method="ozon_pvz",
            status="confirmed", payment_status=orders_repository.PAID, ozon_posting="0001-1",
            details={"address": "Краснодар", "ozon_point_id": 11, "ozon_point_address": POINT,
                     "recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
                     "recipient_email": "ivanov@mail.ru", "delivery_label": f"Ozon, пункт выдачи: {POINT}"},
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        ))
        await session.commit()


OFFER = (
    "Оформить как в прошлый раз?\n"
    "• Те Гуань Инь (тест) × 1 — 1500 ₽\n"
    f"Доставка: пункт выдачи Ozon, {POINT} — 121 ₽\n"
    "Срок: ≈ 7 дней: 1 день соберём и сдадим, 6 дней в пути у Ozon\n"
    "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru\n"
    "Итого: 1621 ₽\n"
    "К нему можно добавить Да Хун Пао — 1500 ₽, до бесплатной доставки как раз не хватает 1500 ₽.\n"
    "Нажмите «Оформить» или ответьте «оформить» — пришлю ссылку на оплату. Если что-то поменять — напишите."
)


@pytest.fixture(autouse=True)
def offer_first(monkeypatch):
    # Здесь — прежний путь с «Оформить»: мгновенный счёт проверяет
    # tests/test_returning_instant.py.
    monkeypatch.setattr(settings, "returning_instant_invoice_enabled", False)


async def test_one_message_with_everything(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}])]
    await say("хочу ещё те гуань инь", 1)
    text, board = world["sent"][-1]
    assert text == OFFER
    labels = [b["action"]["label"] for row in board["buttons"] for b in row]
    assert labels == ["Оформить", "Изменить", "Добавить Да Хун Пао"]
    # Ничего не записано без «Оформить»: ни доставки, ни получателя.
    draft = await state.get_draft(PEER)
    assert draft.delivery_method is None and "recipient_name" not in draft.details
    assert world["payments"] == []


async def test_checkout_button_sends_invoice(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}])]
    await say("хочу ещё те гуань инь", 1)
    ok = world["sent"][-1][1]["buttons"][0][0]["action"]
    await say(ok["label"], 2, json.loads(ok["payload"]))
    text, board = world["sent"][-1]
    assert world["payments"] == [1] and "Итого: 1621 ₽" in text
    assert f"Доставка: пункт выдачи Ozon, {POINT} — 121 ₽" in text
    assert board["buttons"][0][0]["action"]["label"] == "Оплатить 1621 ₽"
    async with clean() as session:
        events = (await session.execute(select(FunnelEvent.event).where(OWN_EVENTS).order_by(FunnelEvent.id))).scalars().all()
    assert events == ["button:offer_ok", "invoice_auto"]


async def test_text_yes_goes_through_accept_offer(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}])]
    await say("хочу ещё те гуань инь", 1)
    world["script"] = [tool_use("accept_offer")]
    await say("да", 2)
    text, board = world["sent"][-1]
    # Кнопку клиент видит — ссылки в тексте нет: ВК помечает её подозрительной.
    assert world["payments"] == [1] and "Оплатить — кнопкой ниже 👇" in text and "yoomoney" not in text
    assert board["buttons"][0][0]["action"]["link"] == "https://yoomoney.ru/checkout/1"


async def test_add_button_rebuilds_the_offer(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}])]
    await say("хочу ещё те гуань инь", 1)
    add = world["sent"][-1][1]["buttons"][1][0]["action"]
    await say(add["label"], 2, json.loads(add["payload"]))
    text, board = world["sent"][-1]
    assert "• Да Хун Пао × 1 — 1500 ₽" in text and "— бесплатно" in text and "Итого: 3000 ₽" in text
    assert [b["action"]["label"] for b in board["buttons"][0]] == ["Оформить", "Изменить"]


async def test_unavailable_point_is_said_and_asked_again(clean, world, monkeypatch):
    await past_order(clean)

    async def refuse(draft, point_id):
        raise RuntimeError("point is not available")

    monkeypatch.setattr(conversation, "_ozon_price", refuse)
    results = []
    real = conversation._execute_tool

    async def spy(peer_id, name, tool_input):
        result = await real(peer_id, name, tool_input)
        results.append(result.tool_result)
        return result

    monkeypatch.setattr(conversation, "_execute_tool", spy)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}]),
                       said("Прошлый пункт сейчас не работает…")]
    await say("хочу ещё те гуань инь", 1)
    assert "не принимает посылки" in results[0] and "Иванов Иван" in results[0]
    assert templates.ASK_WHERE in results[0]
    assert "offer" not in (await state.get_draft(PEER)).details


async def test_flag_off_keeps_old_questions(clean, world, monkeypatch):
    monkeypatch.setattr(settings, "returning_one_question_enabled", False)
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}]),
                       said("Отправить туда же?")]
    await say("хочу ещё те гуань инь", 1)
    assert world["sent"][-1][0] == "Отправить туда же?"
    assert "offer" not in (await state.get_draft(PEER)).details
