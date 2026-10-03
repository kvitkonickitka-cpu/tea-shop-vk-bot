"""Funnel v3, задача 1: постоянному клиенту — сводка сразу со ссылкой."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core import worktime
from app.core.config import settings
from app.messages.models import FunnelEvent
from app.modules.dialog.models import ConversationMessage
from app.modules.orders import conversation, repository as orders_repository, state
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import watch, yookassa_client
from tests.test_auto_invoice import said, tool_use
from tests.test_returning_client import POINT, past_order
from tests.test_vk_buttons import PEER, say, world  # noqa: F401

WANT = [{"name": "Те Гуань Инь (тест)", "quantity": 1}]

INSTANT = (
    "Как в прошлый раз — проверьте, всё ли верно:\n"
    "• Те Гуань Инь (тест) × 1 — 1500 ₽\n"
    f"Доставка: пункт выдачи Ozon, {POINT} — 121 ₽\n"
    "Срок: ≈ 7 дней: 1 день соберём и сдадим, 6 дней в пути у Ozon\n"
    "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru\n"
    "Итого: 1621 ₽\n"
    "\n"
    "Оплатить: https://yoomoney.ru/checkout/1\n"
    "Ссылка действует 60 минут. После оплаты пришлём чек на ivanov@mail.ru и сразу передадим заказ в доставку.\n"
    f"Условия покупки, доставки и возврата: {settings.conditions_url}\n"
    "К нему можно добавить Да Хун Пао — 1500 ₽, до бесплатной доставки как раз не хватает 1500 ₽.\n"
    "Оплатите по ссылке — или напишите, что поменять."
)


def labels(board):
    return [[b["action"]["label"] for b in row] for row in board["buttons"]]


async def events(db):
    async with db() as session:
        return (await session.execute(select(FunnelEvent.event).order_by(FunnelEvent.id))).scalars().all()


async def test_link_comes_in_the_first_message(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    assert len(world["sent"]) == 1  # одна реплика бота на одну клиента
    text, board = world["sent"][-1]
    assert text == INSTANT
    assert labels(board) == [["Оплатить 1621 ₽"], ["Изменить", "Добавить Да Хун Пао"]]
    assert world["payments"] == [1]
    assert await events(clean) == ["invoice_returning"]
    # Черновика нет — он стал заказом со ссылкой.
    assert await state.get_draft(PEER) is None
    live = await orders_repository.live_invoice_order(PEER)
    assert live is not None and live.total == 1621


async def test_other_city_keeps_the_questions(clean, world, monkeypatch):
    await past_order(clean)
    results = []
    real = conversation._execute_tool

    async def spy(peer_id, name, tool_input):
        result = await real(peer_id, name, tool_input)
        results.append((name, result.tool_result))
        return result

    monkeypatch.setattr(conversation, "_execute_tool", spy)
    world["script"] = [
        tool_use("propose_order", items=WANT, delivery_hint="в Москву"),
        said("Записала Те Гуань Инь. В Москву — подскажите улицу, где удобно забрать?"),
    ]
    await say("хочу ещё Те Гуань Инь, но в Москву", 1)
    assert world["payments"] == []
    name, result = results[0]
    assert name == "propose_order" and "«в Москву»" in result and "Прошлую доставку не предлагай" in result
    draft = await state.get_draft(PEER)
    assert draft is not None and draft.delivery_method is None and "offer" not in draft.details
    assert world["sent"][-1][0].startswith("Записала Те Гуань Инь. В Москву")


async def test_other_recipient_keeps_delivery_but_asks_who(clean, world, monkeypatch):
    await past_order(clean)
    results = []
    real = conversation._execute_tool

    async def spy(peer_id, name, tool_input):
        result = await real(peer_id, name, tool_input)
        results.append(result.tool_result)
        return result

    monkeypatch.setattr(conversation, "_execute_tool", spy)
    world["script"] = [tool_use("propose_order", items=WANT, recipient_hint="на маму"),
                       said("Туда же? И пришлите данные мамы.")]
    await say("хочу ещё те гуань инь на маму", 1)
    assert world["payments"] == []
    assert "Получатель в этот раз другой: «на маму»" in results[0]


async def test_add_button_reissues_the_link(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    add = world["sent"][-1][1]["buttons"][1][1]["action"]
    await say(add["label"], 2, json.loads(add["payload"]))
    text, board = world["sent"][-1]
    assert world["payments"] == [1, 2]
    assert "• Да Хун Пао × 1 — 1500 ₽" in text and "— бесплатно" in text and "Итого: 3000 ₽" in text
    assert "Оплатить: https://yoomoney.ru/checkout/2" in text
    assert labels(board)[0] == ["Оплатить 3000 ₽"]
    async with clean() as session:
        rows = (await session.execute(select(OrderPayment).order_by(OrderPayment.attempt))).scalars().all()
    assert [r.attempt for r in rows] == [1, 2] and rows[0].closed_at is not None and rows[1].closed_at is None
    assert await events(clean) == ["invoice_returning", "button:add_more", "invoice_auto"]
    # Номер заказа тот же.
    async with clean() as session:
        assert len((await session.execute(select(Order).where(Order.peer_id == PEER))).scalars().all()) == 2


async def test_change_button_goes_to_the_model(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    edit = world["sent"][-1][1]["buttons"][1][0]["action"]
    world["script"] = [said("Что поменять — пункт, получателя или состав?")]
    await say(edit["label"], 2, json.loads(edit["payload"]))
    assert world["sent"][-1][0] == "Что поменять — пункт, получателя или состав?"
    assert world["model"]  # нажатие дошло до модели текстом


async def test_old_add_button_after_payment_is_stale(clean, world):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    add = world["sent"][-1][1]["buttons"][1][1]["action"]
    async with clean() as session:
        await session.execute(update(Order).where(Order.payment_id == "pay-1").values(
            payment_status=orders_repository.PAID))
        await session.commit()
    world["script"] = [said("Заказ уже оплачен 🙂")]
    await say(add["label"], 2, json.loads(add["payload"]))
    assert world["payments"] == [1]
    assert any("неактуальна" in text for text, _ in world["sent"])


async def test_reminders_follow_the_common_rules(clean, world, monkeypatch):
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: False)
    order = await orders_repository.live_invoice_order(PEER)
    pending = yookassa_client.Payment(id="pay-1", status="pending", paid=False,
                                      confirmation_url="https://yoomoney.ru/checkout/1",
                                      receipt_registration="", test=False, amount=1621)
    later = datetime.now(timezone.utc) + timedelta(minutes=26)
    # Клиент писал только что — молчим.
    assert await watch._remind(order, pending, later) is None
    async with clean() as session:
        await session.execute(update(ConversationMessage).values(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=40)))
        await session.commit()
    assert await watch._remind(order, pending, later) == "payment_reminder_1"
    assert "ждёт оплаты" in world["sent"][-1][0]


async def test_flag_off_keeps_the_offer(clean, world, monkeypatch):
    monkeypatch.setattr(settings, "returning_instant_invoice_enabled", False)
    await past_order(clean)
    world["script"] = [tool_use("propose_order", items=WANT)]
    await say("хочу ещё те гуань инь", 1)
    text, board = world["sent"][-1]
    assert text.startswith("Оформить как в прошлый раз?") and world["payments"] == []
    assert text.endswith("Нажмите «Оформить» или ответьте «оформить» — пришлю ссылку на оплату. "
                         "Если что-то поменять — напишите.")
    assert labels(board)[0] == ["Оформить", "Изменить"]
