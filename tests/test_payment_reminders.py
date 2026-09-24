"""Задача 2: напоминания о неоплаченном счёте и закрытие счёта."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import worktime
from app.messages import templates
from app.messages.models import ClientNotice
from app.modules.dialog import history as dialog_history
from app.modules.orders import state
from app.modules.orders.models import Order
from app.modules.payment import service as payment_service, watch, yookassa_client

PEER = 4100


def payment(status="pending", url="https://yoomoney.ru/checkout/pay/1"):
    return yookassa_client.Payment(
        id="pay-r", status=status, paid=status == "succeeded", confirmation_url=url,
        receipt_registration="pending", test=True, amount=917.0,
    )


async def make_order(db, *, created_minutes_ago: int, **fields) -> Order:
    created = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    async with db() as session:
        order = Order(
            peer_id=PEER,
            items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
            items_total=800, delivery_cost=117, total=917,
            delivery_method="ozon_pvz", status=payment_service.STATUS_AWAITING_PAYMENT,
            payment_id="pay-r", payment_status="pending",
            details={"order_key": "vk4100-1", "recipient_name": "Иванов Иван",
                     "recipient_phone": "79181234567", "ozon_point_id": 42},
            created_at=created,
            **fields,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        return order


@pytest.fixture
def sent(monkeypatch):
    box: list[str] = []

    async def fake_send(peer_id, text, random_id=None):
        box.append(text)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", fake_send)
    return box


@pytest.fixture
def daytime(monkeypatch):
    """Полдень по Москве: не тихие часы и рабочее время."""
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: False)
    monkeypatch.setattr(worktime, "is_working", lambda moment=None: True)


async def test_first_reminder_after_the_interval(clean, sent, daytime):
    order = await make_order(clean, created_minutes_ago=95)
    now = datetime.now(timezone.utc)

    kind = await watch._remind(order, payment(), now)
    assert kind == templates.REMINDER_1
    assert "ждёт оплаты" in sent[-1] and "yoomoney" in sent[-1]

    # Отметка стоит — второй тик молчит.
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.reminder_1_sent_at is not None
    assert await watch._remind(fresh, payment(), now) is None
    assert len(sent) == 1


async def test_no_reminder_before_the_interval(clean, sent, daytime):
    order = await make_order(clean, created_minutes_ago=30)
    assert await watch._remind(order, payment(), datetime.now(timezone.utc)) is None
    assert sent == []


async def test_no_reminder_while_the_client_is_talking(clean, sent, daytime):
    order = await make_order(clean, created_minutes_ago=95)
    await dialog_history.append_message(PEER, "user", "а когда отправите?")

    assert await watch._remind(order, payment(), datetime.now(timezone.utc)) is None
    assert sent == []


async def test_no_reminder_after_the_manager_replied(clean, sent, daytime):
    order = await make_order(clean, created_minutes_ago=95)
    await dialog_history.append_message(
        PEER, "assistant", "Иван, я вам помогу", author=dialog_history.AUTHOR_MANAGER
    )

    assert await watch._remind(order, payment(), datetime.now(timezone.utc)) is None
    assert sent == []


async def test_quiet_hours_postpone_the_reminder(clean, sent, monkeypatch):
    order = await make_order(clean, created_minutes_ago=95)
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: True)

    assert await watch._remind(order, payment(), datetime.now(timezone.utc)) is None
    assert sent == []
    # Отметку не ставим: утром напоминание должно уйти.
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.reminder_1_sent_at is None


async def test_second_reminder_names_the_deadline(clean, sent, daytime):
    # За три часа до закрытия счёта: второе напоминание уже пора.
    minutes = 24 * 60 - 180
    order = await make_order(clean, created_minutes_ago=minutes,
                             reminder_1_sent_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)

    kind = await watch._remind(order, payment(), now)
    assert kind == templates.REMINDER_2
    assert "перестанет работать в" in sent[-1]
    assert worktime.hhmm(watch.expires_at(order)) in sent[-1]


async def test_second_reminder_skipped_when_night_eats_it(clean, sent, monkeypatch):
    """Тихие часы кончатся позже, чем закроется счёт — напоминать нечего."""
    minutes = 24 * 60 - 120
    order = await make_order(clean, created_minutes_ago=minutes,
                             reminder_1_sent_at=datetime.now(timezone.utc))
    monkeypatch.setattr(worktime, "is_quiet", lambda moment=None: True)
    monkeypatch.setattr(
        worktime, "quiet_until",
        lambda moment=None: datetime.now(timezone.utc) + timedelta(hours=9),
    )

    assert await watch._remind(order, payment(), datetime.now(timezone.utc)) is None
    assert sent == []
    # Отметка ставится, чтобы не пересчитывать это каждые пять минут.
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.reminder_2_sent_at is not None


async def test_no_reminder_when_payment_is_not_pending(clean, sent, daytime):
    order = await make_order(clean, created_minutes_ago=95)
    assert await watch._remind(order, payment(status="canceled"), datetime.now(timezone.utc)) is None
    assert await watch._remind(order, payment(status="succeeded"), datetime.now(timezone.utc)) is None
    assert sent == []


async def test_expired_invoice_closes_and_returns_the_draft(clean, sent, monkeypatch):
    order = await make_order(clean, created_minutes_ago=25 * 60)
    canceled: list[str] = []

    async def fake_cancel(payment_id):
        canceled.append(payment_id)
        raise yookassa_client.YooKassaError("HTTP 400 — payment can not be canceled")

    monkeypatch.setattr(yookassa_client, "cancel_payment", fake_cancel)
    await state.clear_draft(PEER)

    await watch._close_invoice(order, payment())

    # Заказ закрыт, отказ ЮKassa в отмене не помешал.
    async with clean() as session:
        fresh = await session.get(Order, order.id)
    assert fresh.status == watch.STATUS_UNPAID
    assert canceled == ["pay-r"]

    # Черновик вернулся на подтверждение и всё сохранил.
    draft = await state.get_draft(PEER)
    assert draft is not None and draft.stage == "awaiting_confirmation"
    assert draft.items[0]["name"] == "Те Гуань Инь"
    assert draft.delivery_method == "ozon_pvz" and draft.delivery_cost == 117
    assert draft.details["recipient_name"] == "Иванов Иван"
    assert draft.details["ozon_point_id"] == 42
    # Номер заказа сброшен: иначе повторное подтверждение вернуло бы тот же
    # закрытый платёж по ключу идемпотентности.
    assert "order_key" not in draft.details

    assert "Счёт по заказу" in sent[-1] and "новую ссылку" in sent[-1]


async def test_expired_invoice_keeps_a_newer_draft(clean, sent, monkeypatch):
    order = await make_order(clean, created_minutes_ago=25 * 60)
    monkeypatch.setattr(yookassa_client, "cancel_payment", lambda payment_id: None)

    from app.modules.orders.state import OrderDraft

    fresh_draft = OrderDraft(items=[{"name": "Да Хун Пао", "quantity": 1, "price": 1100}],
                             items_total=1100.0, stage="collecting")
    await state.set_draft(PEER, fresh_draft)

    await watch._close_invoice(order, payment(status="canceled"))

    draft = await state.get_draft(PEER)
    assert draft.items[0]["name"] == "Да Хун Пао", "затёрли новый черновик клиента"


async def test_new_confirmation_makes_a_new_idempotence_key(clean):
    """Повторное подтверждение обязано дать другой ключ, иначе вернётся старый платёж."""
    old_key = "vk4100-1"
    new_key = "vk4100-2"
    assert yookassa_client.idempotence_key(old_key) != yookassa_client.idempotence_key(new_key)


async def test_client_hears_about_closing_only_once(clean, sent, monkeypatch):
    order = await make_order(clean, created_minutes_ago=25 * 60)
    monkeypatch.setattr(yookassa_client, "cancel_payment", lambda payment_id: None)
    await state.clear_draft(PEER)

    await watch._close_invoice(order, payment(status="canceled"))
    await watch._close_invoice(order, payment(status="canceled"))

    assert len([text for text in sent if "Счёт по заказу" in text]) == 1
    async with clean() as session:
        row = await session.get(ClientNotice, (f"order:{order.id}", templates.PAYMENT_EXPIRED))
    assert row is not None and row.sent_at is not None
