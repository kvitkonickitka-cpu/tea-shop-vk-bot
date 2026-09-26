"""Клиент отменяет заказ сам: неоплаченный — без менеджера."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.orders import (
    cancellation,
    conversation,
    repository as orders_repository,
    shipping,
    state,
)
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, webhook, yookassa_client

PEER = 9700


async def make_order(db, **fields) -> Order:
    values = dict(
        peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 100}],
        items_total=100, delivery_cost=121, total=221, delivery_method="ozon_pvz",
        status=payment_service.STATUS_AWAITING_PAYMENT,
        payment_id="pay-1", payment_status="pending",
        details={"order_key": "vk9700-1", "recipient_name": "Иванов Иван",
                 "recipient_phone": "79181234567", "recipient_email": "a@b.ru",
                 "ozon_point_id": 42},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        await session.refresh(order)
    if order.payment_id:
        await orders_repository.register_payment(
            order.id, order.payment_id, attempt=1, status="pending", amount=221.0
        )
    return order


async def fresh(db, order_id: int) -> Order:
    async with db() as session:
        return await session.get(Order, order_id)


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
    monkeypatch.setattr(cancellation.order_chat, "send", to_manager)
    monkeypatch.setattr(webhook.order_chat, "send", to_manager)
    monkeypatch.setattr(webhook.shipping, "register", register)
    monkeypatch.setattr(yookassa_client, "create_refund", create_refund)
    monkeypatch.setattr(yookassa_client, "cancel_payment", cancel)
    return box


def paid(payment_id: str) -> yookassa_client.Payment:
    return yookassa_client.Payment(
        id=payment_id, status="succeeded", paid=True, confirmation_url="",
        receipt_registration="pending", test=False, amount=221.0,
    )


async def test_unpaid_order_is_canceled_without_a_manager(clean, world):
    order = await make_order(clean)

    execution = await conversation._execute_cancel_order(PEER)

    assert (await fresh(clean, order.id)).status == cancellation.STATUS_CANCELED
    # Ответ готовый, без второго захода к Claude и без эскалации.
    assert execution.client_reply and f"№{order.id}" in execution.client_reply
    async with clean() as session:
        attempt = await session.get(OrderPayment, "pay-1")
    assert attempt.closed_at is not None
    assert world["canceled"] == ["pay-1"]
    assert len(world["manager"]) == 1 and "отменён клиентом" in world["manager"][0]


async def test_expired_invoice_with_restored_draft(clean, world):
    """Счёт истёк, черновик вернулся на подтверждение — отменяется и то и другое."""
    order = await make_order(clean, status=payment_service.STATUS_UNPAID)
    await state.set_draft(PEER, OrderDraft(
        items=list(order.items), items_total=100, delivery_method="ozon_pvz",
        delivery_cost=121, details={"order_id": order.id}, stage="awaiting_confirmation",
    ))

    execution = await conversation._execute_cancel_order(PEER)

    assert await state.get_draft(PEER) is None
    assert (await fresh(clean, order.id)).status == cancellation.STATUS_CANCELED
    assert execution.client_reply


async def test_draft_only(clean, world):
    await state.set_draft(PEER, OrderDraft(
        items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 100}],
        items_total=100, stage="awaiting_delivery",
    ))

    execution = await conversation._execute_cancel_order(PEER)

    assert await state.get_draft(PEER) is None
    assert execution.client_reply == "Хорошо, заказ не оформляю. Если передумаете — напишите 🙂"
    assert world["manager"] == []


async def test_paid_order_goes_to_the_manager(clean, world):
    order = await make_order(clean, status="paid", payment_status="succeeded")

    execution = await conversation._execute_cancel_order(PEER)

    assert (await fresh(clean, order.id)).status == "paid"
    # Готового ответа нет: модель должна позвать менеджера сама.
    assert execution.client_reply is None
    assert "escalate_to_manager" in execution.tool_result
    assert world["canceled"] == [] and world["manager"] == []


async def test_nothing_to_cancel(clean, world):
    execution = await conversation._execute_cancel_order(PEER)
    assert execution.client_reply is None
    assert "Отменять нечего" in execution.tool_result


async def test_payment_after_cancel_is_refunded_not_shipped(clean, world):
    """Клиент отменил, а потом заплатил по старой ссылке: деньги назад, посылки нет."""
    order = await make_order(clean)
    await cancellation.cancel_for_client(PEER)

    result = await webhook.handle_paid(paid("pay-1"))

    assert result["возврат"] == "ref-auto"
    assert world["registered"] == []
    assert world["refunds"][0]["full"] is True
    after = await fresh(clean, order.id)
    assert after.status == cancellation.STATUS_CANCELED
    assert after.payment_status != orders_repository.PAID
    assert any("отменённому заказу" in text for text in world["client"])

    # ЮKassa повторяет уведомление — второй раз ничего не возвращаем.
    again = await webhook.handle_paid(paid("pay-1"))
    assert again["действий"] == "нет, уже возвращён"
    assert len(world["refunds"]) == 1


async def test_payment_first_then_cancel_keeps_the_order(clean, world):
    """Деньги пришли раньше отмены — заказ оплачен, отмена его не трогает."""
    order = await make_order(clean)
    await webhook.handle_paid(paid("pay-1"))

    outcome = await cancellation.cancel_for_client(PEER)

    assert outcome.canceled == [] and outcome.paid == [order.id]
    assert (await fresh(clean, order.id)).payment_status == orders_repository.PAID


async def test_invoice_closing_does_not_revive_a_canceled_order(clean, world):
    await make_order(clean)
    await cancellation.cancel_for_client(PEER)
    canceled_payment = yookassa_client.Payment(
        id="pay-1", status="canceled", paid=False, confirmation_url="",
        receipt_registration="", test=False, amount=221.0,
        cancellation_party="yoo_money", cancellation_reason="expired_on_confirmation",
    )

    result = await webhook._on_canceled(canceled_payment)

    assert result["действий"] == "нет, заказ отменён клиентом"
    assert await state.get_draft(PEER) is None
    assert not any(t.startswith("Срок счёта") for t in world["client"])


def test_cancel_is_offered_on_every_stage():
    for stage in (None, "awaiting_delivery", "awaiting_confirmation"):
        names = [tool["name"] for tool in conversation._tools_for_stage(stage)]
        assert "cancel_order" in names and names[-1] == "escalate_to_manager"


async def test_other_clients_orders_are_untouched(clean, world):
    other = await make_order(clean, peer_id=PEER + 1, payment_id="pay-other")
    await cancellation.cancel_for_client(PEER)
    assert (await fresh(clean, other.id)).status == payment_service.STATUS_AWAITING_PAYMENT
    async with clean() as session:
        rows = (await session.execute(select(OrderPayment))).scalars().all()
    assert all(row.closed_at is None for row in rows)
