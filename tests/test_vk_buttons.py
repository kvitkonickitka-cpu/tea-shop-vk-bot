"""Задача 4: кнопки ВК — лимиты, поддержка приложения, нажатия, старые кнопки."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import keyboard as keyboards, templates
from app.messages.models import FunnelEvent
from app.modules.catalog import service as catalog_service
from app.modules.dialog import history as dialog_history, inbound
from app.modules.orders import conversation, state
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, yookassa_client
from tests.test_auto_invoice import said, tool_use
from tests.test_pickup_choice import KRD

PEER = 9930
FULL = {"inline_keyboard": True, "keyboard": True,
        "button_actions": ["text", "open_link", "callback", "location", "vkpay", "open_app"]}


def test_limits():
    many = [[keyboards.text_button(str(i), {"a": "x"}) for i in range(7)] for _ in range(8)]
    board = keyboards.inline(many)
    assert len(board["buttons"]) == 2 and sum(len(r) for r in board["buttons"]) == 10
    assert all(len(r) <= 5 for r in board["buttons"])
    label = keyboards.text_button("Добавить " + "очень длинное название чая " * 3, {"a": "add"})
    assert len(label["action"]["label"]) == 40
    with pytest.raises(ValueError):
        keyboards.text_button("x", {"a": "x", "pad": "я" * 300})


def test_support_check():
    pay = keyboards.inline([[keyboards.link_button("Оплатить 917 ₽", "https://pay")]])
    assert keyboards.supports(FULL, pay)
    assert not keyboards.supports(None, pay)
    assert not keyboards.supports({"inline_keyboard": True, "button_actions": ["text"]}, pay)
    assert not keyboards.supports({"inline_keyboard": False, "button_actions": ["text", "open_link"]}, pay)


@pytest.fixture
def world(monkeypatch):
    box = {"sent": [], "payments": [], "model": [], "script": []}

    async def send(peer_id, text, random_id=None, keyboard=None):
        box["sent"].append((text, json.loads(keyboard) if keyboard else None))

    async def create_payment(draft, order_key, attempt=1):
        box["payments"].append(attempt)
        return yookassa_client.Payment(
            id=f"pay-{len(box['payments'])}", status="pending", paid=False,
            confirmation_url=f"https://yoomoney.ru/checkout/{len(box['payments'])}",
            receipt_registration="", test=False, amount=draft.items_total + (draft.delivery_cost or 0),
        )

    async def converse(messages, system_prompt, tools):
        box["model"].append(messages[-1]["content"])
        return box["script"].pop(0) if box["script"] else said("Хорошо 🙂")

    async def picked(draft, city, hint=""):
        from app.modules.delivery import ozon_quote
        return ozon_quote.Picked(KRD, len(KRD), len(KRD), True)

    async def price(draft, point_id):
        return NS(total=121.0, days=6)

    monkeypatch.setattr("app.modules.dialog.vk_client.send_message", send)
    monkeypatch.setattr("app.messages.client.vk_client.send_message", send)
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", create_payment)
    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(conversation.ozon_quote, "is_ready", lambda: True)
    monkeypatch.setattr(conversation, "_ozon_points", picked)
    monkeypatch.setattr(conversation, "_ozon_price", price)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [
        {"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True, "recommended": ["Да Хун Пао"]},
        {"name": "Да Хун Пао", "price": 1500, "in_stock": True, "recommended": []}])
    monkeypatch.setattr(settings, "free_delivery_threshold", "3000")
    return box


async def say(text: str, n: int, payload: dict | None = None, info=FULL):
    message = {"peer_id": PEER, "text": text, "conversation_message_id": n}
    if payload is not None:
        message["payload"] = json.dumps(payload)
    await inbound.accept(f"ev{n}", message, info)


async def listing(recipient: bool = True):
    details = {"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
               "recipient_email": "ivanov@mail.ru"} if recipient else {}
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery", details=details))
    await conversation._execute_set_delivery_method(PEER, {"method": "ozon_pvz", "address": "Краснодар"})


async def test_points_get_buttons_and_a_hint(clean, world):
    await listing()
    world["script"] = [said("Пункты: 1) … 2) … Какой удобнее?")]
    await say("а какие пункты?", 1)
    text, board = world["sent"][-1]
    assert text.endswith(templates.POINTS_HINT)
    labels = [row[0]["action"]["label"] for row in board["buttons"]]
    assert labels == ["1. Ставропольская улица, 230", "2. Ставропольская улица, 159",
                      "3. Красная улица, 176", "4. Северная улица, 326"]
    assert json.loads(board["buttons"][1][0]["action"]["payload"])["a"] == "pt"


async def test_no_buttons_without_client_support(clean, world):
    await listing()
    world["script"] = [said("Пункты: 1) …")]
    await say("а какие пункты?", 1, info={"inline_keyboard": False, "button_actions": []})
    text, board = world["sent"][-1]
    assert board is None and templates.POINTS_HINT not in text


async def test_point_button_with_known_recipient_sends_invoice(clean, world):
    await listing()
    version = (await state.get_draft(PEER)).details["version"]
    await say("2. Ставропольская улица, 159", 1, {"a": "pt", "n": 2, "v": version})
    text, board = world["sent"][-1]
    assert world["model"] == [] and world["payments"] == [1]
    assert "Доставка: пункт выдачи Ozon, Краснодар, Ставропольская улица, 159" in text
    assert board["buttons"][0][0]["action"] == {
        "type": "open_link", "label": "Оплатить 1621 ₽", "link": "https://yoomoney.ru/checkout/1"}
    async with clean() as session:
        events = (await session.execute(select(FunnelEvent.event).order_by(FunnelEvent.id))).scalars().all()
    assert events == ["button:pt", "invoice_auto"]
    history = await dialog_history.get_history(PEER)
    assert history[-2]["content"] == "2. Ставропольская улица, 159" and history[-1]["content"] == text


async def test_point_button_without_recipient_asks_for_data(clean, world):
    await listing(recipient=False)
    version = (await state.get_draft(PEER)).details["version"]
    await say("1. Ставропольская улица, 230", 1, {"a": "pt", "n": 1, "v": version})
    assert world["sent"][-1][0] == (
        "Записала пункт: Краснодар, Ставропольская улица, 230. Доставка — 121 ₽, итого 1621 ₽.\n"
        + templates.ASK_RECIPIENT
    )


async def test_stale_button_goes_to_model(clean, world):
    await listing()
    await say("1. Ставропольская улица, 230", 1, {"a": "pt", "n": 1, "v": 1})
    assert world["sent"][0][0] == "Эта кнопка уже неактуальна."
    assert world["model"] == ["1. Ставропольская улица, 230"]
    async with clean() as session:
        events = (await session.execute(select(FunnelEvent.event))).scalars().all()
    assert "button_stale" in events


async def test_forged_order_button_is_stale(clean, world):
    await say("Прислать новую ссылку", 1, {"a": "new_link", "o": 999})
    assert world["sent"][0][0] == "Эта кнопка уже неактуальна." and world["payments"] == []


async def test_email_typo_yes_button(clean, world):
    await listing(recipient=False)
    draft = await state.get_draft(PEER)
    draft.details.update(ozon_point_id=11, ozon_point_address=KRD[0].address)
    draft.details.pop("shown_points")
    await state.set_draft(PEER, draft)
    world["script"] = [tool_use("set_recipient", name="Иванов Иван", phone="89001234567", email="ivanov@yandex.ry"),
                       said("Может, ivanov@yandex.ru?")]
    await say("Иванов Иван, 89001234567, ivanov@yandex.ry", 1)
    text, board = world["sent"][-1]
    assert text.endswith(templates.EMAIL_HINT)
    yes = board["buttons"][0][0]["action"]
    assert yes["label"] == "Да, ivanov@yandex.ru"
    await say(yes["label"], 2, json.loads(yes["payload"]))
    assert world["payments"] == [1] and "Получатель: Иванов Иван, +79001234567, ivanov@yandex.ru" in world["sent"][-1][0]


async def test_upsell_button_adds_item(clean, world):
    world["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}]),
                       said("Записала! Добавить Да Хун Пао? Куда везти?")]
    await say("хочу те гуань инь", 1)
    text, board = world["sent"][-1]
    add = board["buttons"][0][0]["action"]
    assert add["label"] == "Добавить Да Хун Пао"
    await say(add["label"], 2, json.loads(add["payload"]))
    assert world["sent"][-1][0] == (
        "Добавила Да Хун Пао. Товаров на 3000 ₽.\nДоставка для вас будет бесплатной 🙂\n"
        + templates.ASK_WHERE
    )
    assert [i["name"] for i in (await state.get_draft(PEER)).items] == ["Те Гуань Инь (тест)", "Да Хун Пао"]


async def test_new_link_button_reissues(clean, world):
    await listing()
    version = (await state.get_draft(PEER)).details["version"]
    await say("1", 1, {"a": "pt", "n": 1, "v": version})
    async with clean() as session:
        order = (await session.execute(select(Order))).scalar_one()
    # Счёт истёк: догляд закрыл его и вернул черновик.
    await payment_service.restore_draft(order)
    await say("Прислать новую ссылку", 2, {"a": "new_link", "o": order.id})
    assert world["payments"] == [1, 2] and f"Заказ №{order.id}" in world["sent"][-1][0]
