"""Пункт 3 ревью: менеджер выставляет счёт по номеру заказа."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.messages import manager as manager_messages
from app.modules.orders import conversation, repository as orders_repository, state
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, yookassa_client

PEER = 9600


async def make_order(db, **fields) -> Order:
    values = dict(
        peer_id=PEER, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800, delivery_cost=117, total=917, delivery_method="ozon_pvz",
        status=payment_service.STATUS_PAYMENT_FAILED, payment_status=None,
        details={"order_key": "vk9600-1", "recipient_name": "Иванов Иван",
                 "recipient_phone": "79181234567", "recipient_email": "a@b.ru", "ozon_point_id": 42,
                 "delivery_label": "Ozon, пункт выдачи: Ставропольская 230"},
        created_at=datetime.now(timezone.utc),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


@pytest.fixture
def kassa(monkeypatch):
    box: dict = {"created": []}

    async def fake_create(draft, order_key, attempt=1):
        box["created"].append({"order_key": order_key, "attempt": attempt,
                               "items": list(draft.items)})
        return yookassa_client.Payment(
            id=f"pay-manual-{attempt}", status="pending", paid=False,
            confirmation_url=f"https://yoomoney.ru/checkout/pay/{attempt}",
            receipt_registration="pending", test=True, amount=917.0,
        )

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", fake_create)
    return box


async def test_invoice_for_a_complete_order(clean, kassa):
    order = await make_order(clean)

    payment, note = await payment_service.issue_for_order(order)

    assert payment is not None and payment.confirmation_url.startswith("https://yoomoney.ru")
    assert "попытка 1" in note
    # Заказ ждёт оплаты, платёж записан как попытка — значит оплата по нему
    # заведёт отправление обычным путём.
    async with clean() as session:
        fresh = await session.get(Order, order.id)
        attempt_row = await session.get(OrderPayment, payment.id)
    assert fresh.status == "awaiting_payment" and fresh.payment_id == payment.id
    assert attempt_row is not None and attempt_row.attempt == 1


async def test_second_invoice_uses_the_next_attempt(clean, kassa):
    order = await make_order(clean)
    await payment_service.issue_for_order(order)

    async with clean() as session:
        fresh = await session.get(Order, order.id)
    payment, note = await payment_service.issue_for_order(fresh)

    assert kassa["created"][-1]["attempt"] == 2
    assert kassa["created"][-1]["order_key"] == "vk9600-1", "номер заказа менять не надо"
    assert "попытка 2" in note
    assert yookassa_client.idempotence_key("vk9600-1", 1) != yookassa_client.idempotence_key(
        "vk9600-1", 2
    )


@pytest.mark.parametrize(
    "broken,expected",
    [
        ({"items": []}, "состав заказа"),
        ({"details": {"recipient_phone": "79181234567", "ozon_point_id": 1}}, "ФИО"),
        ({"details": {"recipient_name": "Иванов"}}, "телефон получателя"),
        ({"details": {"recipient_name": "Иванов", "recipient_phone": "123",
                      "ozon_point_id": 1}}, "телефон получателя целиком"),
        ({"details": {"recipient_name": "Иванов", "recipient_phone": "79181234567"}},
         "пункт выдачи Ozon"),
        ({"delivery_method": None}, "способ доставки"),
        ({"details": {"recipient_name": "Иванов", "recipient_phone": "79181234567",
                      "ozon_point_id": 1}}, "почта для чека"),
        ({"details": {"recipient_name": "Иванов", "recipient_phone": "79181234567",
                      "recipient_email": "a.b.ru", "ozon_point_id": 1}},
         "почта для чека целиком"),
    ],
)
async def test_incomplete_order_is_refused(clean, kassa, broken, expected):
    order = await make_order(clean, **broken)

    payment, note = await payment_service.issue_for_order(order)

    assert payment is None
    assert expected in note
    assert kassa["created"] == [], "пошли в ЮKassa с неполным заказом"


async def test_paid_order_gets_no_second_invoice(clean, kassa):
    order = await make_order(clean, status="paid", payment_status="succeeded")

    payment, note = await payment_service.issue_for_order(order)

    assert payment is None and "уже оплачен" in note


async def test_failed_payment_still_saves_the_order(clean, monkeypatch):
    """Менеджеру нужен номер заказа, чтобы выставить счёт командой."""
    sent: list[str] = []

    async def to_manager(text, chat_id=None):
        sent.append(text)

    async def refuse(draft, order_key, attempt=1):
        raise yookassa_client.YooKassaError("HTTP 400 — invalid_request")

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", refuse)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)

    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800.0, delivery_cost=117.0, delivery_method="ozon_pvz",
        delivery_label="Ozon, пункт выдачи",
        details={"recipient_name": "Иванов Иван", "recipient_phone": "79181234567",
                 "recipient_email": "a@b.ru",
                 "ozon_point_id": 42},
        stage="awaiting_confirmation",
    )
    await state.set_draft(PEER, draft)

    result = await conversation._execute_confirm_order(PEER)

    assert "выставить оплату не получилось" in result.client_reply
    # Заказ сохранён, и менеджеру подсказана команда с его номером.
    async with clean() as session:
        from sqlalchemy import select

        orders = (await session.execute(select(Order).where(Order.peer_id == PEER))).scalars().all()
    assert len(orders) == 1
    assert orders[0].status == payment_service.STATUS_PAYMENT_FAILED
    assert f"orders/{orders[0].id}/invoice" in sent[-1]


async def test_hint_says_when_there_is_no_order(clean, monkeypatch):
    sent: list[str] = []

    async def to_manager(text, chat_id=None):
        sent.append(text)

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)

    draft = OrderDraft(items=[], items_total=0.0, stage="confirmed")
    await conversation._escalate_for_payment(PEER, draft, None, "ЮKassa не ответила")

    assert "оформлять вручную" in sent[-1]
