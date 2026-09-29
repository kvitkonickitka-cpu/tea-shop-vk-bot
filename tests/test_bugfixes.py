"""Задача 0: суммы в шаблонах, разметка в запасном канале, «стоп» посреди заказа."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.messages import client as client_messages, manager as manager_messages, marketing, templates
from app.modules.dialog import history as dialog_history
from app.modules.orders import conversation

PEER = 9990
MOMENT = datetime(2026, 9, 28, 14, 35, tzinfo=timezone.utc)
ORDER = NS(
    id=128, peer_id=PEER, total=917, items_total=800, delivery_cost=117,
    items=[{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 400}],
    details={"recipient_email": "ivanov@mail.ru"}, created_at=MOMENT, paid_at=MOMENT,
    ozon_posting="0123-4567-8", cdek_uuid=None, delivery_method="ozon_pvz",
)
# Сумма объектом, как её отдаёт SDK ЮKassa, — так она и попадала в шаблон целиком.
PAYMENT = NS(id="2f5c…a71", amount=NS(value="917.00"), status="succeeded")
REFUND = NS(id="3a1b…c02", amount={"value": "917.00", "currency": "RUB"}, status="succeeded")

SAMPLES = {
    "order": ORDER, "payment": PAYMENT, "refund": REFUND, "items": ORDER.items,
    "address": "Краснодар, Ставропольская, 230", "approximate": False, "carrier": "СДЭК",
    "carrier_status": "СДЭК: Не вручен (NOT_DELIVERED)", "cdek": False, "consent": "yes",
    "count": 3, "day": date(2026, 10, 5), "delivery_label": "в пункт выдачи Ozon",
    "email": "ivanov@mail.ru", "error": "refund is not allowed", "event_type": "paid",
    "expires": MOMENT, "expires_at": MOMENT, "items_total": 800, "link": "https://vk.com/gim1?sel=1",
    "minutes": 60, "number": "1234567890", "order_id": 128, "paid_at": MOMENT,
    "payment_status": "pending", "phone": "+79001234567", "postamat": False,
    "posting": "0123-4567-8", "question": "Есть ли опт?", "raw": "+79001234567",
    "reason": "нет данных в ассортименте", "receipt_email": "ivanov@mail.ru",
    "receipt_status": "pending", "refund_amount": NS(value="300.00"),
    "refunded_amount": {"value": "917.00"}, "storage_until": date(2026, 10, 5),
    "text": "Чай отличный", "threshold_gap": 200, "total": 917, "tracking_url": "https://www.cdek.ru/ru/tracking",
    "urgent": True, "url": "https://example.org/pack/1", "value": NS(value="917.00"),
    "waited_minutes": 120, "weeks": 3,
}


def _render_all():
    for name, func in inspect.getmembers(templates, inspect.isfunction):
        if func.__module__ != templates.__name__ or name.startswith("_"):
            continue
        params = inspect.signature(func).parameters
        missing = [p for p in params if p not in SAMPLES]
        assert not missing, f"{name}: нет примерных данных для {missing} — допиши SAMPLES"
        yield name, func(**{p: SAMPLES[p] for p in params})


def test_every_template_renders_clean():
    rendered = dict(_render_all())
    assert len(rendered) > 40
    for name, text in rendered.items():
        for bad in ("namespace(", "None", "{", "}"):
            assert bad not in str(text), f"{name}: в тексте «{bad}»: {text}"
    assert "на 917 ₽" in rendered["manager_double_payment"]
    assert "вернули 917 ₽" in rendered["double_payment"]


@pytest.mark.parametrize("value, expected", [
    (917, "917"), (917.0, "917"), ("917.00", "917"), (917.5, "917,50"), ("917.05", "917,05"),
    (1_500_000, "1500000"), ({"value": "12.30"}, "12,30"), (NS(value="917.00"), "917"),
    (None, "—"),
])
def test_amount_format(value, expected):
    assert templates.amount(value) == expected


async def test_admin_fallback_is_plain_and_telegram_keeps_html(monkeypatch):
    card = "↩️ <b>Заказ №128</b>\nПлатёж a &lt; b &amp; c\n📦 <a href=\"https://x.ru/pack\">Собрать</a>"
    to_telegram, to_vk = [], []

    async def telegram(text, chat_id=None):
        to_telegram.append(text)

    async def vk(peer_id, text, random_id=None):
        to_vk.append(text)

    async def admin_id(raw):
        return 42

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", telegram)
    monkeypatch.setattr(manager_messages.vk_client, "send_message", vk)
    monkeypatch.setattr(manager_messages.vk_client, "resolve_user_id", admin_id)
    monkeypatch.setattr(settings, "admin_vk_id", "42")

    await manager_messages._send(card, None)
    assert to_telegram == [card]

    await manager_messages._fallback_to_admin(1, "order_card", 128, card, "timeout")
    sent = to_vk[0]
    assert "<b>" not in sent and "</a>" not in sent and "&lt;" not in sent
    assert "↩️ Заказ №128\nПлатёж a < b & c\n📦 Собрать: https://x.ru/pack" in sent


@pytest.fixture
def model(monkeypatch):
    calls = []

    async def converse(messages, system_prompt, tools):
        calls.append(messages[-1]["content"])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Хорошо, заказ сохранён 🙂")])

    async def to_client(peer_id, text, random_id=None):
        pass

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(client_messages.vk_client, "send_message", to_client)
    return calls


async def test_stop_after_sales_reminder_is_opt_out(clean, model):
    await client_messages.send(
        peer_id=PEER, ref="draft:1", event_type=templates.DRAFT_NUDGE, text="Заказ ждёт вас"
    )
    reply = await conversation.handle_turn(PEER, "стоп")
    assert reply == templates.marketing_stopped()
    assert model == [] and await marketing.is_opted_out(PEER)


async def test_stop_mid_order_goes_to_model(clean, model):
    # Последнее, что бот написал сам, — напоминание об оплате: это не реклама.
    await client_messages.send(
        peer_id=PEER, ref="order:5", event_type="reminder_1", text="Напоминаю про заказ"
    )
    assert await conversation.handle_turn(PEER, "стоп") == "Хорошо, заказ сохранён 🙂"
    assert model == ["стоп"] and not await marketing.is_opted_out(PEER)


async def test_stop_after_client_already_answered_reminder(clean, model):
    await client_messages.send(
        peer_id=PEER, ref="draft:2", event_type=templates.DRAFT_NUDGE, text="Заказ ждёт вас"
    )
    await dialog_history.append_exchange(PEER, "а какой пункт?", "Ставропольская, 230")
    await conversation.handle_turn(PEER, "стоп")
    assert model == ["стоп"] and not await marketing.is_opted_out(PEER)


def test_pause_rule_in_prompt():
    assert "«Стоп», «подождите», «секунду» посреди оформления — это пауза" in conversation.order_flow_prompt()
