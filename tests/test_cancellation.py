"""Задача 3.1: отмена платежа и что из неё слышит клиент."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.messages import templates
from app.modules.orders import state
from app.modules.orders.models import Order
from app.modules.payment import service as payment_service, webhook, yookassa_client

PEER = 5200


def canceled(party: str, reason: str) -> yookassa_client.Payment:
    return yookassa_client.Payment(
        id="pay-c", status="canceled", paid=False, confirmation_url="",
        receipt_registration="", test=True, amount=917.0,
        cancellation_party=party, cancellation_reason=reason,
    )


async def make_order(db, **fields) -> Order:
    values = dict(
        peer_id=PEER, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800, delivery_cost=117, total=917, delivery_method="ozon_pvz",
        status=payment_service.STATUS_AWAITING_PAYMENT,
        payment_id="pay-c", payment_status="pending",
        details={"order_key": "vk5200-1", "recipient_phone": "79181234567"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    values.update(fields)
    async with db() as session:
        order = Order(**values)
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []

    async def fake_send(peer_id, text, random_id=None):
        box.append(text)

    async def no_cancel(payment_id):
        raise yookassa_client.YooKassaError("payment can not be canceled")

    monkeypatch.setattr("app.messages.client.vk_client.send_message", fake_send)
    monkeypatch.setattr(yookassa_client, "cancel_payment", no_cancel)
    return box


@pytest.mark.parametrize(
    "party,reason,decision",
    [
        ("yoo_money", "general_decline", payment_service.ON_CANCEL_DECLINED),
        ("payment_network", "insufficient_funds", payment_service.ON_CANCEL_DECLINED),
        ("yoo_money", "3d_secure_failed", payment_service.ON_CANCEL_DECLINED),
        ("merchant", "canceled_by_merchant", payment_service.ON_CANCEL_BY_MERCHANT),
        ("yoo_money", "expired_on_confirmation", payment_service.ON_CANCEL_EXPIRED),
        ("yoo_money", "expired_on_capture", payment_service.ON_CANCEL_EXPIRED),
        ("", "", payment_service.ON_CANCEL_DECLINED),
    ],
)
def test_cancellation_is_classified(party, reason, decision):
    assert payment_service.decide_on_cancel(party, reason) == decision


async def test_bank_refusal_offers_another_card(clean, sent):
    order = await make_order(clean)
    await state.clear_draft(PEER)

    result = await webhook._on_canceled(canceled("payment_network", "insufficient_funds"))

    assert result["решение"] == payment_service.ON_CANCEL_DECLINED
    assert sent and "не прошёл" in sent[-1] and "другой картой" in sent[-1]
    # Заказ закрыт, а собранный черновик вернулся: клиенту хватит «да».
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.status == payment_service.STATUS_UNPAID
    draft = await state.get_draft(PEER)
    assert draft is not None and draft.stage == "awaiting_confirmation"


async def test_expired_cancellation_stays_silent(clean, sent):
    """Про закрытый счёт клиент уже слышал от догляда — второй раз молчим."""
    order = await make_order(clean)
    await state.clear_draft(PEER)

    result = await webhook._on_canceled(canceled("yoo_money", "expired_on_confirmation"))

    assert result["решение"] == payment_service.ON_CANCEL_EXPIRED
    assert sent == []
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.status == payment_service.STATUS_UNPAID


async def test_merchant_cancellation_stays_silent(clean, sent):
    await make_order(clean)
    await state.clear_draft(PEER)

    result = await webhook._on_canceled(canceled("merchant", "canceled_by_merchant"))

    assert result["решение"] == payment_service.ON_CANCEL_BY_MERCHANT
    assert sent == []


async def test_repeated_notification_changes_nothing(clean, sent):
    await make_order(clean)
    await state.clear_draft(PEER)

    await webhook._on_canceled(canceled("yoo_money", "general_decline"))
    second = await webhook._on_canceled(canceled("yoo_money", "general_decline"))

    assert second["действий"] == "нет, счёт уже закрыт"
    assert len([text for text in sent if "не прошёл" in text]) == 1


async def test_partial_refund_names_the_refunded_sum(clean, sent, monkeypatch):
    order = await make_order(clean, status="paid", payment_status="succeeded")

    async def fake_refund(refund_id):
        return yookassa_client.Refund(
            id=refund_id, payment_id="pay-c", status="succeeded", amount=300.0
        )

    async def fake_chat(order_, text):
        return True

    monkeypatch.setattr(webhook.yookassa_client, "get_refund", fake_refund)
    monkeypatch.setattr(webhook.order_chat, "send", fake_chat)

    await webhook.handle({"event": "refund.succeeded", "object": {"id": "ref-9"}})

    assert sent and "возврат 300 ₽" in sent[-1]
    assert "917" not in sent[-1], "назвали сумму заказа вместо суммы возврата"


async def test_shipment_trouble_text_follows_working_hours(monkeypatch):
    from app.core import worktime

    order = type("O", (), {"id": 7})()
    monkeypatch.setattr(worktime, "is_working", lambda moment=None: True)
    assert "сегодня" in templates.shipment_trouble(order)
    monkeypatch.setattr(worktime, "is_working", lambda moment=None: False)
    assert "в ближайший рабочий день" in templates.shipment_trouble(order)
