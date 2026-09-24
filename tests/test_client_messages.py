"""Инфраструктура сообщений клиенту: один раз, в историю, с отметкой."""

from __future__ import annotations

import types

import pytest

from app.messages import client as client_messages, templates
from app.modules.dialog import history as dialog_history


def fake_order(order_id: int = 1, peer_id: int = 1000):
    return types.SimpleNamespace(
        id=order_id, peer_id=peer_id, total=917.0,
        details={"recipient_email": "a@b.ru", "recipient_phone": "79181234567"},
    )


@pytest.fixture
def sent(monkeypatch):
    box: list[dict] = []

    async def fake_send(peer_id, text, random_id=None):
        box.append({"peer_id": peer_id, "text": text, "random_id": random_id})

    monkeypatch.setattr(client_messages.vk_client, "send_message", fake_send)
    return box


async def test_sends_once_per_event(clean, sent):
    order = fake_order()
    ref = client_messages.order_ref(order.id)

    assert await client_messages.send(
        peer_id=order.peer_id, ref=ref, event_type=templates.PAID, text="раз"
    )
    # Второй вызов по тому же событию не должен ничего отправлять.
    assert not await client_messages.send(
        peer_id=order.peer_id, ref=ref, event_type=templates.PAID, text="два"
    )
    assert [message["text"] for message in sent] == ["раз"]

    # А другое событие по тому же заказу — отправляется.
    assert await client_messages.send(
        peer_id=order.peer_id, ref=ref, event_type=templates.REFUNDED, text="возврат"
    )
    assert len(sent) == 2


async def test_random_id_is_derived_from_event(clean, sent):
    order = fake_order(7)
    ref = client_messages.order_ref(order.id)
    await client_messages.send(
        peer_id=order.peer_id, ref=ref, event_type=templates.PAID, text="текст"
    )
    assert sent[-1]["random_id"] == client_messages.random_id(ref, templates.PAID)
    assert 0 < sent[-1]["random_id"] <= 0x7FFFFFFF
    # Одно и то же событие — одно и то же значение, между запусками тоже.
    assert client_messages.random_id("order:7", templates.PAID) == 99037474


async def test_sent_message_lands_in_history(clean, sent):
    order = fake_order(9, peer_id=2002)
    await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=templates.PAID,
        text="Оплата получена",
    )
    history = await dialog_history.get_history(order.peer_id)
    assert history == [{"role": "assistant", "content": "Оплата получена"}]


async def test_vk_refusal_is_reported_and_not_retried(clean, monkeypatch):
    order = fake_order(11, peer_id=3003)
    told: list[str] = []

    async def refuse(peer_id, text, random_id=None):
        raise RuntimeError("VK API error: {'error_code': 901}")

    async def on_failure(error):
        told.append(error)

    monkeypatch.setattr(client_messages.vk_client, "send_message", refuse)

    ok = await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=templates.PAID,
        text="Оплата получена",
        on_failure=on_failure,
    )
    assert ok is False
    assert told and "901" in told[0]

    # В историю неотправленное не попадает.
    assert await dialog_history.get_history(order.peer_id) == []

    # Повторный вызов не пытается отправить снова: событие уже занято.
    async def must_not_be_called(peer_id, text, random_id=None):
        raise AssertionError("повторная отправка после отказа ВК")

    monkeypatch.setattr(client_messages.vk_client, "send_message", must_not_be_called)
    assert not await client_messages.send(
        peer_id=order.peer_id,
        ref=client_messages.order_ref(order.id),
        event_type=templates.PAID,
        text="Оплата получена",
    )

    # Отметка сохранила ошибку — по ней видно, что попытка была.
    from app.messages.models import ClientNotice

    async with clean() as session:
        row = await session.get(ClientNotice, (client_messages.order_ref(11), templates.PAID))
    assert row is not None and row.sent_at is None and row.attempts == 1
    assert "901" in row.last_error


def test_templates_read_well():
    order = fake_order(128)
    assert "917 руб" in templates.paid(order, email="a@b.ru")
    assert "917.0" not in templates.paid(order, email="a@b.ru")
    assert "a@b.ru" in templates.paid(order, email="a@b.ru")
    # Без почты чек уходит на телефон, и номер показан маской.
    on_phone = templates.paid(order, phone="79181234567")
    assert "+7 *** ***-45-67" in on_phone and "79181234567" not in on_phone
    assert "0123" in templates.paid(order, posting="0123-4567-8")
    assert "СДЭК" in templates.paid(order, cdek=True)
