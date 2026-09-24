"""Задача 1: инструкция по заказу и причина эскалации по оплате."""

from __future__ import annotations

import pytest

from app.modules.orders import conversation
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service


def test_payment_step_follows_the_flag(monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    with_kassa = conversation.order_flow_prompt()

    monkeypatch.setattr(payment_service, "is_enabled", lambda: False)
    without_kassa = conversation.order_flow_prompt()

    assert with_kassa != without_kassa
    # При включённой кассе ссылку отдаёт инструмент, а не менеджер.
    assert "выставит счёт" in with_kassa
    assert "её пришлёт менеджер" not in with_kassa
    # При выключенной — наоборот.
    assert "пришлёт менеджер" in without_kassa


def test_prompt_has_no_leftover_placeholders(monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    prompt = conversation.order_flow_prompt()
    assert "{payment_step}" not in prompt
    assert "{map_url}" not in prompt
    assert conversation.CDEK_OFFICES_MAP_URL in prompt


@pytest.mark.parametrize(
    "reason",
    [
        "Счёт выставить не удалось: YooKassaError: HTTP 400 — invalid_request",
        "ЮKassa не дала точного ответа, счёт мог создаться",
    ],
)
async def test_escalation_names_the_real_reason(clean, monkeypatch, reason):
    """Менеджер должен читать причину отказа, а не «модуль не подключён»."""
    sent: list[str] = []

    async def fake_send(text, chat_id=None):
        sent.append(text)

    monkeypatch.setattr(conversation.telegram_client, "send_message", fake_send)

    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800.0,
        delivery_cost=117.0,
        delivery_label="Ozon, пункт выдачи",
        stage="confirmed",
    )
    await conversation._escalate_for_payment(555, draft, 42, reason)

    assert sent, "менеджеру ничего не ушло"
    message = sent[-1]
    assert reason.split(":")[0] in message
    assert "Модуль оплаты не подключён" not in message
    assert "Заказ №42" in message


async def test_escalation_reason_defaults_to_kassa_state(clean, monkeypatch):
    """Без причины текст всё равно не врёт про подключение кассы."""
    sent: list[str] = []

    async def fake_send(text, chat_id=None):
        sent.append(text)

    monkeypatch.setattr(conversation.telegram_client, "send_message", fake_send)
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)

    draft = OrderDraft(items=[], items_total=0.0, stage="confirmed")
    await conversation._escalate_for_payment(556, draft, None)

    assert "не подключен" not in sent[-1].lower()
    assert "смотреть лог" in sent[-1]
