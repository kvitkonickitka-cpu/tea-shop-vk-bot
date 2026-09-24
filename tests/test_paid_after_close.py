"""Пункт 1.3 ревью: оплата по закрытому счёту и двойная оплата."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.messages import templates
from app.modules.orders import repository as orders_repository, shipping
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import service as payment_service, webhook, yookassa_client

PEER = 9300


def paid(payment_id: str, amount: float = 917.0) -> yookassa_client.Payment:
    return yookassa_client.Payment(
        id=payment_id, status="succeeded", paid=True, confirmation_url="",
        receipt_registration="pending", test=True, amount=amount,
    )


async def make_order(db, *, status: str, payment_id: str, payment_status: str) -> Order:
    async with db() as session:
        order = Order(
            peer_id=PEER, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
            items_total=800, delivery_cost=117, total=917, delivery_method="ozon_pvz",
            status=status, payment_id=payment_id, payment_status=payment_status,
            details={"order_key": "vk9300-1", "recipient_name": "Иванов Иван",
                     "recipient_phone": "79181234567", "ozon_point_id": 42},
            created_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


@pytest.fixture
def world(monkeypatch):
    box: dict = {"client": [], "manager": [], "registered": [], "refunds": [], "canceled": []}

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    async def to_manager(order, text):
        box["manager"].append(text)
        return True

    async def register(**kwargs):
        box["registered"].append(kwargs)
        return shipping.Registered(ozon_posting="0123-4567-8")

    async def create_refund(**kwargs):
        box["refunds"].append(kwargs)
        return yookassa_client.Refund(
            id="ref-auto", payment_id=kwargs["payment_id"], status="succeeded",
            amount=kwargs["amount"],
        )

    async def cancel(payment_id):
        box["canceled"].append(payment_id)
        raise yookassa_client.YooKassaError("payment can not be canceled")

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(webhook.order_chat, "send", to_manager)
    monkeypatch.setattr(webhook.shipping, "register", register)
    monkeypatch.setattr(yookassa_client, "create_refund", create_refund)
    monkeypatch.setattr(yookassa_client, "cancel_payment", cancel)
    return box


async def test_payment_on_a_closed_invoice_is_accepted(clean, world):
    """Клиент заплатил по закрытому счёту — деньги настоящие, принимаем."""
    order = await make_order(
        clean, status=payment_service.STATUS_UNPAID, payment_id="pay-1",
        payment_status="pending",
    )
    await orders_repository.register_payment(
        order.id, "pay-1", attempt=1, status="pending", amount=917.0
    )
    await orders_repository.close_payment("pay-1")

    result = await webhook.handle_paid(paid("pay-1"))

    assert result["оплачен закрытый счёт"] is True
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.payment_status == orders_repository.PAID
    assert fresh.ozon_posting == "0123-4567-8"
    # Отправление заведено ровно одно.
    assert len(world["registered"]) == 1
    # Клиент получает обычное сообщение об оплате.
    assert "Оплата получена" in world["client"][-1]
    # Менеджеру — с пометкой про закрытый счёт.
    assert any("по закрытому счёту" in text for text in world["manager"])


async def test_second_payment_is_refunded(clean, world):
    """По оплаченному заказу пришли вторые деньги — возвращаем сами."""
    order = await make_order(
        clean, status="paid", payment_id="pay-2", payment_status="succeeded",
    )
    for attempt, payment_id in ((1, "pay-1"), (2, "pay-2")):
        await orders_repository.register_payment(
            order.id, payment_id, attempt=attempt, status="pending", amount=917.0
        )

    result = await webhook.handle_paid(paid("pay-1"))

    assert result["возврат"] == "ref-auto"
    # Вторая посылка не заводится.
    assert world["registered"] == []
    assert world["refunds"][0]["payment_id"] == "pay-1"
    assert world["refunds"][0]["amount"] == 917.0
    # Чек возврата собран из позиций заказа.
    assert world["refunds"][0]["items"][0]["name"] == "Те Гуань Инь"
    assert "вернули 917 ₽" in world["client"][-1]
    assert any("повторная оплата возвращена" in text for text in world["manager"])

    # Наш возврат не должен объявлять заказ возвращённым: посылка едет.
    async with clean() as session:
        row = await session.get(OrderPayment, "pay-1")
        fresh = await session.get(Order, order.id)
    assert row.refund_id == "ref-auto"
    assert fresh.status == "paid"


async def test_our_refund_does_not_reverse_the_order(clean, world, monkeypatch):
    """Уведомление refund.succeeded по нашему возврату не трогает заказ."""
    order = await make_order(
        clean, status="paid", payment_id="pay-2", payment_status="succeeded",
    )
    await orders_repository.register_payment(
        order.id, "pay-1", attempt=1, status="succeeded", amount=917.0
    )
    await orders_repository.close_payment("pay-1", refund_id="ref-auto")

    async def get_refund(refund_id):
        return yookassa_client.Refund(
            id="ref-auto", payment_id="pay-1", status="succeeded", amount=917.0
        )

    monkeypatch.setattr(webhook.yookassa_client, "get_refund", get_refund)

    result = await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-auto"}})

    assert result["действий"] == "нет, это возврат второй оплаты"
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.status == "paid", "наш возврат остановил доставку"
    assert world["client"] == []


async def test_managers_refund_still_reverses_the_order(clean, world, monkeypatch):
    """А возврат, сделанный менеджером в кабинете, работает как раньше."""
    order = await make_order(
        clean, status="paid", payment_id="pay-1", payment_status="succeeded",
    )
    await orders_repository.register_payment(
        order.id, "pay-1", attempt=1, status="succeeded", amount=917.0
    )

    async def get_refund(refund_id):
        return yookassa_client.Refund(
            id="ref-manager", payment_id="pay-1", status="succeeded", amount=300.0
        )

    monkeypatch.setattr(webhook.yookassa_client, "get_refund", get_refund)

    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-manager"}})

    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.status == webhook.STATUS_REFUNDED
    assert "возврат 300 ₽" in world["client"][-1]


async def test_two_succeeded_at_once_make_one_shipment(clean, world):
    """Одновременный приход двух succeeded: одна посылка, один возврат."""
    order = await make_order(
        clean, status=payment_service.STATUS_AWAITING_PAYMENT, payment_id="pay-2",
        payment_status="pending",
    )
    for attempt, payment_id in ((1, "pay-1"), (2, "pay-2")):
        await orders_repository.register_payment(
            order.id, payment_id, attempt=attempt, status="pending", amount=917.0
        )

    results = await asyncio.gather(
        webhook.handle_paid(paid("pay-1")),
        webhook.handle_paid(paid("pay-2")),
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, Exception)], results

    # Ровно одна посылка и ровно один возврат.
    assert len(world["registered"]) == 1, world["registered"]
    assert len(world["refunds"]) == 1, world["refunds"]

    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.payment_status == orders_repository.PAID
    # Возвращён тот платёж, который пришёл вторым — не тот, что приняли.
    assert world["refunds"][0]["payment_id"] != fresh.payment_id


async def test_failed_refund_calls_the_manager(clean, world, monkeypatch):
    order = await make_order(
        clean, status="paid", payment_id="pay-2", payment_status="succeeded",
    )
    await orders_repository.register_payment(
        order.id, "pay-1", attempt=1, status="pending", amount=917.0
    )

    async def refuse(**kwargs):
        raise yookassa_client.YooKassaError("HTTP 400 — refund is not allowed")

    monkeypatch.setattr(yookassa_client, "create_refund", refuse)

    result = await webhook.handle_paid(paid("pay-1"))

    assert result["действий"] == "возврат не удался"
    assert any("НЕ возвращена" in text for text in world["manager"])
    assert "Разбираемся с возвратом" in world["client"][-1]


async def test_repeat_notification_of_the_same_payment_does_nothing(clean, world):
    order = await make_order(
        clean, status=payment_service.STATUS_AWAITING_PAYMENT, payment_id="pay-1",
        payment_status="pending",
    )
    await orders_repository.register_payment(
        order.id, "pay-1", attempt=1, status="pending", amount=917.0
    )

    await webhook.handle_paid(paid("pay-1"))
    second = await webhook.handle_paid(paid("pay-1"))

    assert second["действий"] == "нет, уже обработан"
    assert len(world["registered"]) == 1
    assert world["refunds"] == []
