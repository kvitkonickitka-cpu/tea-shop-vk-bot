"""Ассистент в чате Ops: кому отвечает и что уходит в модель.

Главное — перехватчик: вопрос владельца с данными клиента, тревога и
результат инструмента с чужим именем и телефоном в тексте ошибки проходят
весь путь до запроса к Anthropic, и ни одно значение из тестовых данных
туда не доезжает. Номера заказов и peer_id при этом остаются — они
ассистенту и нужны.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.modules.dialog import claude_client, telegram_client
from app.modules.ops import journal
from app.modules.ops.assistant import redact, service, tools
from app.privacy import detect

ME = {"id": 777, "username": "claudeagent_notification_bot", "is_bot": True}
OPS = "-1004495345064"

# Всё личное — ни одно не должно попасть в запрос к модели.
SECRETS = [
    "Иванов", "Иванова", "Ивановой", "Петров", "Петрову", "Надежда", "Сергеевна", "Анна",
    "9001234567", "900) 123", "123-45-67", "8 900 123 45 67",
    "ivanov@mail.ru", "nadya@yandex.ru",
    "Ленина 5", "кв 3", "кв. 12", "Мира, д. 12",
    "55.7558", "37.6173",
]


@pytest.fixture(autouse=True)
def ops_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_ops_chat_id", OPS)
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "")
    monkeypatch.setattr(settings, "ops_assistant_daily_limit", 60)
    service._threads.clear()
    service._day = ("", 0)


def _stop():
    return detect.stop_list([{"name": "Да Хун Пао", "synonyms": ["Большой красный халат"]}])


def message(text, *, chat=OPS, reply_to=None, sender=1001, mention=True, age=0):
    entities = []
    if mention:
        text = f"@{ME['username']} {text}"
        entities = [{"type": "mention", "offset": 0, "length": len(ME["username"]) + 1}]
    msg = {
        "message_id": 50, "date": int(time.time()) - age, "chat": {"id": int(chat), "type": "supergroup"},
        "from": {"id": sender, "is_bot": False}, "text": text, "entities": entities,
    }
    if reply_to:
        msg["reply_to_message"] = reply_to
    return msg


# --- очистка -------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "почему у Иванова Ивана не прошла оплата",
    "Надежда заберёт сама, оплатила Петрову Анна Сергеевна",
    "клиентка Ивановой звонила с 8 900 123 45 67 и +7 (900) 123-45-67",
    "почта ivanov@mail.ru и nadya@yandex.ru",
    "курьер на Ленина 5, кв 3, а второй — ул. Мира, д. 12, кв. 12",
    "геопозиция 55.7558, 37.6173",
    "в истории [NAME_1] и [PHONE_2]",
])
def test_clean_removes_client_data(raw):
    cleaned, count = redact.clean(raw, _stop())
    assert count > 0
    for secret in SECRETS:
        assert secret not in cleaned, (secret, cleaned)
    assert "_1]" not in cleaned and "_2]" not in cleaned


@pytest.mark.parametrize("raw", [
    "почему заказ 128 не оплачен, peer_id 123456789",
    "СДЭК 5 сбоев за час, Ozon молчит",
    "Что с тиком? Сколько вопросов менеджеру висит?",
    "клиент спрашивал Да Хун Пао, проверь заказ №1042",
    "на сайте 5 ошибок",
])
def test_clean_keeps_identifiers_and_ops_words(raw):
    cleaned, count = redact.clean(raw, _stop())
    assert cleaned == raw, cleaned
    assert count == 0


# --- кому отвечаем -------------------------------------------------------------


@pytest.fixture
def started(monkeypatch):
    seen = []

    async def answer(msg, me):
        seen.append(msg)

    monkeypatch.setattr(service, "_answer_safely", answer)
    return seen


async def test_mention_in_ops_chat_is_a_question(started):
    assert service.handle_update({"update_id": 1, "message": message("что с ЮKassa?")}, ME)


async def test_mention_after_emoji(started):
    text = f"🔥🔥 @{ME['username']} что это?"
    msg = message(text, mention=False)
    # Смещение в UTF-16: каждый огонёк — две единицы, плюс пробел.
    msg["entities"] = [{"type": "mention", "offset": 5, "length": len(ME["username"]) + 1}]
    assert service.handle_update({"update_id": 1, "message": msg}, ME)


async def test_reply_to_the_bot_is_a_question(started):
    reply = {"message_id": 10, "from": ME, "text": "🔎 СДЭК — 2 сбоя"}
    assert service.handle_update({"update_id": 1, "message": message("а что делать?", mention=False, reply_to=reply)}, ME)


async def test_everything_else_is_ignored(started):
    other_reply = {"message_id": 10, "from": {"id": 5, "is_bot": False}, "text": "привет"}
    cases = [
        message("просто разговор", mention=False),
        message("ответ человеку", mention=False, reply_to=other_reply),
        message("@другой_бот вопрос", mention=False),
        message("что с заказом?", chat="-5363112139"),  # чат заказов: тот же бот, но не наш чат
        message("старый вопрос", age=3600),
    ]
    bot_msg = message("бот сам себе")
    bot_msg["from"]["is_bot"] = True
    cases.append(bot_msg)
    for msg in cases:
        assert not service.handle_update({"update_id": 1, "message": msg}, ME), msg["text"]
    assert started == []


async def test_only_allowed_people(started, monkeypatch):
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "42, 43")
    assert not service.handle_update({"update_id": 1, "message": message("вопрос", sender=1001)}, ME)
    assert service.handle_update({"update_id": 2, "message": message("вопрос", sender=42)}, ME)


# --- перехватчик ---------------------------------------------------------------


def tool_use(name, **kwargs):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t-{name}", name=name, input=kwargs)],
              usage=NS(input_tokens=1, output_tokens=1))


def said(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)], usage=NS(input_tokens=1, output_tokens=1))


@pytest.fixture
def wire(monkeypatch):
    box = {"requests": [], "script": [], "sent": [], "redacted": []}

    async def create(**kwargs):
        box["requests"].append(kwargs)
        return box["script"].pop(0)

    async def run(name, arguments):
        # Перевозчик вернул в тексте ошибки имя и телефон получателя.
        return {
            "order": {"id": 1042, "peer_id": 123456789, "status": "confirmed", "payment_status": "succeeded"},
            "client_messages": [{"event_type": "paid", "sent_at": None,
                                 "last_error": "Получатель Иванов Пётр +79001234567 не найден"}],
            "manager_notifications": [{"kind": "carrier_failed",
                                       "last_error": "адрес на Ленина 5, кв 3 не распознан"}],
        }

    async def reply(text, *, chat_id, reply_to):
        box["sent"].append(text)
        return {"message_id": 900 + len(box["sent"])}

    async def typing(chat_id):
        return None

    monkeypatch.setattr(claude_client._client.messages, "create", create)
    monkeypatch.setattr(tools, "run", run)
    monkeypatch.setattr(telegram_client, "reply", reply)
    monkeypatch.setattr(telegram_client, "typing", typing)
    monkeypatch.setattr(journal, "note_pii_redacted", lambda op, count: box["redacted"].append((op, count)))
    return box


async def test_no_client_data_reaches_the_model(wire):
    alert = {
        "message_id": 10, "from": {"id": 999, "is_bot": True},
        "text": "🔎 СДЭК — 1 сбой\n00:24 · создание заказа — HTTP 400 · заказ #1042\n"
                "Получатель Иванова Анна Сергеевна, 8 900 123 45 67, ivanov@mail.ru, ул. Мира, д. 12, кв. 12",
    }
    question = message(
        "почему у Ивановой не ушла посылка? Надежда заберёт, её почта nadya@yandex.ru, геопозиция 55.7558, 37.6173",
        reply_to=alert,
    )
    wire["script"] = [tool_use("order_status", order_id=1042), said("Заказ 1042 оплачен, СДЭК отклонил создание.")]

    await service._answer_safely(question, ME)

    dumped = json.dumps(wire["requests"], ensure_ascii=False, default=str)
    for secret in SECRETS + ["Пётр", "79001234567"]:
        assert secret not in dumped, secret
    # Идентификаторы остаются — без них ассистент бесполезен.
    assert "1042" in dumped and "123456789" in dumped
    # Последний рубеж не сработал: всё сняла очистка ассистента.
    assert wire["redacted"] == []
    assert len(wire["sent"]) == 1
    assert "Заказ 1042 оплачен" in wire["sent"][0]
    assert "скрыто данных клиента" in wire["sent"][0]


async def test_follow_up_keeps_the_thread_without_client_data(wire):
    wire["script"] = [said("Тик отработал 2 минуты назад."), said("Да, очередь пустая.")]
    await service._answer_safely(message("что с расписанием? спросите у Петрова"), ME)
    bot_answer = {"message_id": 901, "from": ME, "text": "Тик отработал 2 минуты назад."}
    await service._answer_safely(message("а очередь менеджеру?", mention=False, reply_to=bot_answer), ME)

    last = wire["requests"][-1]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "user"]
    dumped = json.dumps(wire["requests"], ensure_ascii=False, default=str)
    assert "Петров" not in dumped


async def test_tool_calls_are_capped(wire, monkeypatch):
    monkeypatch.setattr(settings, "ops_assistant_max_tool_calls", 2)
    wire["script"] = [tool_use("schedule_status"), tool_use("schedule_status"), said("Готово.")]
    await service._answer_safely(message("что с расписанием?"), ME)
    assert wire["requests"][-1].get("tool_choice") == {"type": "none"}
    assert "Готово." in wire["sent"][0]


async def test_daily_limit(wire, monkeypatch):
    monkeypatch.setattr(settings, "ops_assistant_daily_limit", 1)
    wire["script"] = [said("Первый ответ.")]
    await service._answer_safely(message("первый"), ME)
    await service._answer_safely(message("второй"), ME)
    assert len(wire["requests"]) == 1
    assert "лимит" in wire["sent"][1]


# --- инструменты: только разрешённые поля --------------------------------------


async def test_order_tool_shows_no_client_fields(db):
    from app.core.database import get_session_factory
    from app.modules.orders.models import Order

    async with get_session_factory()() as session:
        order = Order(
            peer_id=123456789, items=[{"name": "Да Хун Пао", "quantity": 1}], items_total=1000, total=1000,
            delivery_method="ozon", status="confirmed",
            details={"recipient_name": "Иванов Пётр", "recipient_phone": "+79001234567",
                     "recipient_email": "ivanov@mail.ru", "point_address": "ул. Мира, д. 12"},
        )
        session.add(order)
        await session.commit()
        order_id = order.id

    data = await tools.order_status(order_id)
    dumped = json.dumps(data, ensure_ascii=False, default=str)
    assert str(order_id) in dumped and "123456789" in dumped
    for secret in ("Иванов", "79001234567", "ivanov@", "Мира"):
        assert secret not in dumped


async def test_bot_report_is_not_mistaken_for_names(wire):
    report = {"message_id": 11, "from": ME,
              "text": "📊 Сутки бота · 14.10 15:20 – 15.10 15:20 МСК\nАптайм: 100,0 % (1440 из 1440 минут с пульсом)\n"
                      "Ответы: 16, p95 8,8 с\nСбоев сервисов: 0"}
    wire["script"] = [said("Аптайм 100 %.")]
    await service._answer_safely(message("что значит первая строка?", mention=False, reply_to=report), ME)
    dumped = json.dumps(wire["requests"], ensure_ascii=False, default=str)
    assert "Аптайм: 100,0 %" in dumped
    assert "скрыто" not in wire["sent"][0]


async def test_sources_list_what_was_looked_at(wire):
    alert = {"message_id": 10, "from": {"id": 999, "is_bot": True}, "text": "🔎 СДЭК — 1 сбой · заказ #1042"}
    wire["script"] = [tool_use("order_status", order_id=1042), said("**Сбой СДЭКа** по `1042`.\n\n- проверить ключ")]
    await service._answer_safely(message("что делать?", reply_to=alert), ME)
    sent = wire["sent"][0]
    assert "<b>Сбой СДЭКа</b>" in sent and "<code>1042</code>" in sent and "• проверить ключ" in sent
    assert "<b>Источники:</b>" in sent
    assert 'href="https://t.me/c/4495345064/10"' in sent
    assert ">заказ №1042</a>" in sent and "gim240363526?sel=123456789" in sent
    assert "#runbook" in sent


async def test_model_html_is_escaped(wire):
    wire["script"] = [said("<b>жирно</b> и <a href='x'>ссылка</a>")]
    await service._answer_safely(message("тест"), ME)
    assert "&lt;b&gt;жирно" in wire["sent"][0]


async def test_empty_answer_is_asked_again_in_words(wire):
    empty = NS(stop_reason="end_turn", content=[NS(type="thinking", thinking="", signature="x")],
               usage=NS(input_tokens=1, output_tokens=1))
    wire["script"] = [empty, said("Сбоев за сутки нет.")]
    await service._answer_safely(message("что со сбоями?"), ME)
    assert len(wire["requests"]) == 2
    assert wire["requests"][1].get("tool_choice") == {"type": "none"}
    assert wire["requests"][1]["messages"][-1]["role"] == "user"
    assert "Сбоев за сутки нет." in wire["sent"][0] and "пустой ответ" not in wire["sent"][0]
