"""Часть Б, Б7: перехват каждого запроса к Anthropic — персональных данных в нём нет.

Модель здесь настоящая только снаружи: подменён сам HTTP-клиент Anthropic,
а обёртка `claude_client.converse` с последним рубежом работает как в бою.
Поэтому проверка двойная: в запросах нет ни одного значения из тестовых
данных, и последний рубеж ни разу не сработал — значит, всё сняли метки.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import privacy
from app.messages import keyboard as keyboards, manager as manager_messages
from app.modules.dialog import claude_client, history as dialog_history, service as dialog_service
from app.modules.dialog.models import Escalation
from app.modules.ops import journal
from app.modules.orders import conversation, repeat_nudge, state
from app.modules.orders.models import Order, OrderFeedback
from app.modules.orders.state import OrderDraft
from app.privacy import detect
from tests.test_auto_invoice import said, tool_use
from tests.test_returning_client import past_order
from tests.test_storefront_points import ADDRESS, shop, storefront  # noqa: F401
from tests.test_vk_buttons import FULL, PEER, listing, say, world  # noqa: F401

REAL_CONVERSE = claude_client.converse

# Всё личное из сценариев — ни одно не должно доехать до Anthropic.
SECRETS = [
    "Иванов", "ivanov@", "9001234567", "900) 123", "900 123", "123-45-67",
    "Петров", "Пётр", "petrov@", "7654321", "765-43-21", "кв. 5", "кв. 12", "Чистопрудный",
    "Анне Смирновой", "Смирнов", "Иван,",
]


@pytest.fixture
def wire(world, monkeypatch):
    world["requests"] = []
    world["redacted"] = []
    world["manager"] = []

    async def create(**kwargs):
        world["requests"].append(kwargs)
        return world["script"].pop(0) if world["script"] else said("Хорошо 🙂")

    async def to_manager(text, chat_id=None):
        world["manager"].append(text)

    monkeypatch.setattr(conversation.claude_client, "converse", REAL_CONVERSE)
    monkeypatch.setattr(claude_client._client.messages, "create", create)
    monkeypatch.setattr(journal, "note_pii_redacted", lambda op, count: world["redacted"].append((op, count)))
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    return world


def dump(request: dict) -> str:
    return json.dumps(
        {"system": request["system"], "messages": request["messages"]},
        ensure_ascii=False, default=lambda o: getattr(o, "__dict__", str(o)),
    )


def assert_clean(world):
    assert world["requests"], "к модели не обращались — проверять нечего"
    for request in world["requests"]:
        text = dump(request)
        leaked = [value for value in SECRETS if value in text]
        assert not leaked, leaked
        assert privacy.REDACTED not in text
    assert world["redacted"] == []  # последний рубеж не понадобился


def last_user(world) -> str:
    content = world["requests"][-1]["messages"][-1]["content"]
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


async def test_new_client_sends_data_in_one_message(clean, wire):
    await listing(recipient=False)
    wire["script"] = [
        tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар", pickup_point="1"),
        tool_use("set_recipient", name="[NAME_1]", phone="[PHONE_1]", email="[EMAIL_1]"),
    ]
    await say("1, Иванов Иван, 89001234567, ivanov@mail.ru", 1)
    assert wire["requests"][0]["messages"][-1]["content"] == "1, [NAME_1], [PHONE_1], [EMAIL_1]"
    # Клиенту — настоящие значения в сводке со ссылкой.
    text, _ = wire["sent"][-1]
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in text and wire["payments"] == [1]
    async with clean() as session:
        order = (await session.execute(select(Order))).scalar_one()
    assert order.details["recipient_name"] == "Иванов Иван" and order.details["recipient_phone"] == "+79001234567"
    assert_clean(wire)


async def test_new_client_sends_data_in_three_messages(clean, wire):
    await listing(recipient=False)
    wire["script"] = [
        tool_use("set_delivery_method", method="ozon_pvz", address="Краснодар", pickup_point="1"),
        said("Записала пункт. Пришлите ФИО, телефон и почту получателя."),
    ]
    await say("1", 1)
    wire["script"] = [said("Записала [NAME_1]. Теперь телефон и почту.")]
    await say("Иванов Иван", 2)
    assert wire["sent"][-1][0] == "Записала Иванов Иван. Теперь телефон и почту."
    wire["script"] = [said("Спасибо! И почту для чека.")]
    await say("+7 900 123-45-67", 3)
    wire["script"] = [tool_use("set_recipient", name="[NAME_1]", phone="[PHONE_1]", email="[EMAIL_1]")]
    await say("ivanov@mail.ru", 4)
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in wire["sent"][-1][0]
    assert_clean(wire)


async def test_returning_client(clean, wire):
    await past_order(clean)
    await keyboards.remember_client(PEER, FULL)
    wire["script"] = [tool_use("propose_order", items=[{"name": "Те Гуань Инь (тест)", "quantity": 1}])]
    await say("хочу Те Гуань Инь, как в прошлый раз", 1)
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in wire["sent"][-1][0]
    wire["script"] = [said("Пожалуйста! Ссылка выше.")]
    await say("спасибо", 2)
    assert "[NAME_1]" in dump(wire["requests"][-1])  # сводка в истории — с метками
    assert_clean(wire)


async def test_repeat_button(clean, wire):
    await past_order(clean)
    async with clean() as session:
        order = (await session.execute(select(Order))).scalar_one()
        order.delivered_at = datetime.now(timezone.utc) - timedelta(days=22)
        await session.commit()
    await keyboards.remember_client(PEER, FULL)
    from app.core import worktime

    assert (await repeat_nudge.check(datetime.now(worktime.MSK).replace(hour=15, minute=0)))["sent"] == 1
    buttons = {b["action"]["label"]: b["action"] for b in wire["sent"][-1][1]["buttons"][0]}
    await say("Повторить", 5, json.loads(buttons["Повторить"]["payload"]))
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in wire["sent"][-1][0]
    wire["script"] = [said("Да, всё верно 🙂")]
    await say("а получатель верный?", 6)
    assert_clean(wire)


async def test_storefront_with_recipient(clean, shop, wire):
    await storefront(shop)
    wire["script"] = [said("Привезём в пункт [NAME_1] — выберите номер пункта.")]
    await say("а кто получит?", 1)
    assert wire["sent"][-1][0].startswith("Привезём в пункт Петров Пётр — выберите номер пункта.")
    assert ADDRESS not in dump(wire["requests"][-1])
    assert_clean(wire)


async def test_cdek_courier_address(clean, wire, monkeypatch):
    from app.modules.delivery import cdek_client

    asked = []

    async def cdek(draft, method, address, delivery_point=None):
        asked.append(address)
        return cdek_client.Tariff(137, "Посылка склад-дверь", 320.0, 3, 4, 3), 397.72

    monkeypatch.setattr(conversation, "_cdek_delivery", cdek)
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
        stage="awaiting_delivery"))
    await dialog_history.append_exchange(PEER, "курьером", "Курьер СДЭК привезёт до двери. Напишите адрес доставки.")
    wire["script"] = [tool_use("set_delivery_method", method="cdek_courier", address="[ADDR_1]"),
                      said("Курьер — 398 ₽. Пришлите ФИО, телефон и почту.")]
    await say("Москва, Чистопрудный бульвар, д. 5, кв. 12", 1)
    assert asked == ["Москва, Чистопрудный бульвар, д. 5, кв. 12"]  # СДЭК считает по настоящему адресу
    assert wire["requests"][0]["messages"][-1]["content"] == "[ADDR_1]"
    assert_clean(wire)


async def test_escalation_with_phone(clean, wire):
    wire["script"] = [
        tool_use("escalate_to_manager", question="Опт: клиент просит перезвонить на [PHONE_1]", reason="опт"),
        said("Передала менеджеру — он перезвонит."),
    ]
    await say("Хочу оптом, перезвоните мне 89001234567", 1)
    async with clean() as session:
        question = (await session.execute(select(Escalation.question))).scalar_one()
    assert question == "Опт: клиент просит перезвонить на +79001234567"  # менеджеру — значение
    assert any("+79001234567" in card for card in wire["manager"])
    assert_clean(wire)


async def test_manager_reply_with_name(clean, wire):
    await dialog_service.handle_message_reply(
        {"peer_id": PEER, "admin_author_id": 1, "text": "Иван, добрый день! Скидка 10% ваша."}
    )
    wire["script"] = [said("Да, скидка действует 🙂")]
    await say("спасибо, скидка точно есть?", 1)
    assert "[NAME_1], добрый день!" in dump(wire["requests"][-1])
    assert_clean(wire)


async def test_review(clean, wire):
    async with clean() as session:
        order = Order(
            peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}],
            items_total=1500, delivery_cost=121, total=1621, delivery_method="ozon_pvz", status="confirmed",
            payment_status="succeeded", details={}, created_at=datetime.now(timezone.utc) - timedelta(days=6),
            delivered_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(order)
        await session.commit()
    wire["script"] = [
        tool_use("save_feedback", order_id=order.id, publish_consent="unknown",
                 text="Чай отличный! Передайте спасибо [NAME_1], мой телефон [PHONE_1]"),
        said("Спасибо за отзыв!"),
    ]
    await say("Чай отличный! Передайте спасибо Анне Смирновой, мой телефон 89001234567", 1)
    async with clean() as session:
        saved = (await session.execute(select(OrderFeedback.text))).scalar_one()
    assert saved == "Чай отличный! Передайте спасибо Анне Смирновой, мой телефон +79001234567"
    assert_clean(wire)


async def test_timing(clean, capsys):
    """Замер для отчёта: загрузка словаря и время tokenize на сообщение."""
    detect._morph.cache_clear()
    detect.word.cache_clear()
    load = detect.warm_up()
    messages = [
        "1, Иванов Иван, 89001234567, ivanov@mail.ru",
        "Подскажите, чем Да Хун Пао отличается от Те Гуань Инь? Хочу что-то к вечеру.",
        "Краснодар, Ставропольская 230",
        "Меня зовут Анна, а мужа Сергей Петрович, можно на него?",
        "спасибо!",
    ] * 20
    await privacy.tokenize(PEER, "разогрев 89001234567")
    started = time.perf_counter()
    for text in messages:
        await privacy.tokenize(PEER, text, stage=frozenset({privacy.RECIPIENT}))
    per_message = (time.perf_counter() - started) / len(messages) * 1000
    with capsys.disabled():
        print(f"\n[замер] словарь pymorphy3: {load * 1000:.0f} мс; tokenize: {per_message:.2f} мс на сообщение")
    assert load < 3 and per_message < 50
