"""Аналитика, A2: события воронки, их источник и статус попытки оплаты."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.messages import funnel, templates
from app.messages.models import FunnelEvent
from app.modules.dialog import inbound
from app.modules.dialog.models import InboundMessage
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderPayment
from app.modules.payment import service as payment_service, webhook, yookassa_client
from tests.test_paid_after_close import make_order, world  # noqa: F401
from tests.test_vk_buttons import PEER


async def journal(db, peer_id: int | None = None) -> list[tuple[str, str | None, dict | None]]:
    async with db() as session:
        query = select(FunnelEvent).order_by(FunnelEvent.id)
        if peer_id is not None:
            query = query.where(FunnelEvent.peer_id == peer_id)
        rows = (await session.execute(query)).scalars().all()
    return [(row.event, row.source, row.data) for row in rows]


async def test_source_follows_the_turn(clean):
    await funnel.record(1, "a")
    with funnel.source(funnel.BUTTON):
        await funnel.record(1, "b")
        await funnel.record(1, "c", source_=funnel.CODE)
    await funnel.record(1, "d")
    assert [(e, s) for e, s, _ in await journal(clean)] == [
        ("a", "text"), ("b", "button"), ("c", "code"), ("d", "text"),
    ]


async def test_dialog_starts_once_a_day(clean):
    async def came(n: int):
        message = {"peer_id": PEER, "text": "…", "conversation_message_id": n}
        await inbound._note_dialog_start(PEER, f"ev{n}", message)
        await inbound._store(f"ev{n}", message)

    await came(1)
    await came(2)
    assert [e for e, _, _ in await journal(clean)] == ["dialog_start"]
    async with clean() as session:
        await session.execute(update(InboundMessage).values(
            received_at=InboundMessage.received_at - timedelta(hours=25)))
        await session.commit()
    await came(3)
    assert [e for e, _, _ in await journal(clean)] == ["dialog_start", "dialog_start"]


def paid(test: bool) -> yookassa_client.Payment:
    return yookassa_client._to_payment({
        "id": "pay-1", "status": "succeeded", "paid": True, "test": test,
        "amount": {"value": "917.00"}, "income_amount": {"value": "884.90"},
        "payment_method": {"type": "sbp"},
    })


async def test_payment_updates_attempt_and_records_money(clean, world):
    order = await make_order(clean, status=payment_service.STATUS_AWAITING_PAYMENT,
                             payment_id="pay-1", payment_status="pending")
    await orders_repository.register_payment(order.id, "pay-1", attempt=1, status="pending", amount=917.0)

    await webhook.handle_paid(paid(test=False))

    async with clean() as session:
        attempt = await session.get(OrderPayment, "pay-1")
        fresh = await session.get(Order, order.id)
    assert attempt.status == "succeeded" and attempt.payment_method == "sbp"
    assert float(attempt.income_amount) == 884.90 and attempt.updated_at is not None
    assert fresh.is_test is False
    events = await journal(clean)
    assert [(e, s) for e, s, _ in events] == [("payment_succeeded", "yookassa"), ("shipment_created", "code")]
    assert events[0][2] == {"attempt": 1, "amount": 917.0, "income": 884.9, "method": "sbp"}
    assert events[1][2] == {"carrier": "ozon"}


async def test_payment_in_test_shop_marks_the_order(clean, world):
    order = await make_order(clean, status=payment_service.STATUS_AWAITING_PAYMENT,
                             payment_id="pay-1", payment_status="pending")
    await webhook.handle_paid(paid(test=True))
    async with clean() as session:
        assert (await session.get(Order, order.id)).is_test is True


async def test_declined_and_expired_and_canceled_by_shop(clean, world):
    order = await make_order(clean, status=payment_service.STATUS_AWAITING_PAYMENT,
                             payment_id="pay-1", payment_status="pending")
    await orders_repository.register_payment(order.id, "pay-1", attempt=1, status="pending", amount=917.0)
    declined = yookassa_client._to_payment({
        "id": "pay-1", "status": "canceled",
        "cancellation_details": {"party": "payment_network", "reason": "insufficient_funds"},
    })
    await webhook._on_canceled(declined)
    async with clean() as session:
        assert (await session.get(OrderPayment, "pay-1")).status == "canceled"

    pending = yookassa_client.Payment(id="pay-1", status="pending", paid=False, confirmation_url="",
                                      receipt_registration="", test=False, amount=917.0)
    await payment_service.close_invoice(order, pending, notice=templates.PAYMENT_EXPIRED)
    await payment_service.close_invoice(order, pending, notice=None)
    events = await journal(clean)
    assert [(e, s, d) for e, s, d in events] == [
        ("payment_declined", "yookassa", {"reason": "insufficient_funds", "party": "payment_network"}),
        ("invoice_expired", "code", None),
        ("invoice_canceled", "code", None),
    ]
