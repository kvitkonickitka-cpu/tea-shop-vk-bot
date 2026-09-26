"""Почта для чека обязательна: «Чеки от ЮKassa» шлют чек только письмом."""

from __future__ import annotations

import pytest

from app.messages import templates
from app.modules.orders import conversation, state
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service, yookassa_client

PEER = 7400


@pytest.mark.parametrize(
    "raw,valid",
    [
        ("+7 918 123-45-67", True),
        ("8 (918) 123-45-67", True),
        ("79181234567", True),
        ("918 12 34", False),
        ("", False),
        ("телефона нет", False),
    ],
)
def test_phone_validation(raw, valid):
    assert yookassa_client.phone_is_valid(raw) is valid


def test_receipt_carries_only_email():
    """«Чеки от ЮKassa» доставляют чек только письмом: телефон в чек не идёт."""
    customer = yookassa_client.receipt_customer(
        full_name="Иванов Иван", email="a@b.ru", phone="79181234567"
    )
    assert customer == {"full_name": "Иванов Иван", "email": "a@b.ru"}


@pytest.mark.parametrize(
    "raw,valid",
    [("a@b.ru", True), (" ivan.petrov@mail.example.com ", True),
     ("a.b.ru", False), ("a@b", False), ("", False), ("a @b.ru", False)],
)
def test_email_validation(raw, valid):
    assert yookassa_client.email_is_valid(raw) is valid


async def test_payment_without_email_is_refused(monkeypatch):
    """Без почты платёж не выставляем сами, а не ловим отказ ЮKassa."""
    async def must_not_call(*args, **kwargs):
        raise AssertionError("пошли в ЮKassa без почты")

    monkeypatch.setattr(yookassa_client, "_call", must_not_call)

    with pytest.raises(yookassa_client.YooKassaError) as error:
        await yookassa_client.create_payment(
            order_key="vk1-1", items=[{"name": "Чай", "quantity": 1, "price": 800}],
            delivery_cost=0, delivery_label="", email="", phone="79181234567",
            full_name="Иванов", description="Заказ",
        )
    assert "нет почты" in str(error.value)


def _ready_draft(**details) -> OrderDraft:
    base = {"recipient_name": "Иванов Иван", "recipient_phone": "+7 918 123-45-67",
            "ozon_point_id": 42}
    base.update(details)
    return OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800.0, delivery_cost=117.0, delivery_method="ozon_pvz",
        delivery_label="Ozon, пункт выдачи: Ставропольская 230",
        details=base, stage="awaiting_confirmation",
    )


async def test_confirm_order_asks_for_email(clean, monkeypatch):
    async def must_not_create(*args, **kwargs):
        raise AssertionError("счёт без почты")

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", must_not_create)
    await state.set_draft(PEER, _ready_draft())

    result = await conversation._execute_confirm_order(PEER)
    assert "электронная почта" in result.tool_result
    assert result.client_reply is None


async def test_confirm_order_with_email_issues_invoice(clean, monkeypatch):
    created: dict = {}

    async def fake_create(draft, order_key, attempt=1):
        created["email"] = draft.details.get("recipient_email", "")
        return yookassa_client.Payment(
            id="pay-x", status="pending", paid=False,
            confirmation_url="https://yoomoney.ru/checkout/pay/x",
            receipt_registration="pending", test=True, amount=917.0,
        )

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", fake_create)
    await state.set_draft(PEER, _ready_draft(recipient_email="a@b.ru"))

    result = await conversation._execute_confirm_order(PEER)

    assert "Оплатить: https://yoomoney.ru" in result.client_reply
    assert "чек на a@b.ru" in result.client_reply
    assert created["email"] == "a@b.ru"


async def test_confirm_order_asks_for_a_whole_phone(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await state.set_draft(
        PEER, _ready_draft(recipient_phone="918-12-34", recipient_email="a@b.ru")
    )

    result = await conversation._execute_confirm_order(PEER)
    assert "11 цифр" in result.tool_result
    assert result.client_reply is None


async def test_set_recipient_rejects_a_broken_phone(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await state.set_draft(
        PEER,
        OrderDraft(items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
                   delivery_method="ozon_pvz", stage="awaiting_confirmation"),
    )

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Иванов Иван", "phone": "123", "email": "a@b.ru"}
    )
    assert "не похож на российский номер" in answer
    draft = await state.get_draft(PEER)
    assert "recipient_phone" not in draft.details


async def test_set_recipient_rejects_a_broken_email(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await state.set_draft(
        PEER,
        OrderDraft(items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
                   delivery_method="ozon_pvz", stage="awaiting_confirmation"),
    )

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Иванов Иван", "phone": "79181234567", "email": "ivan.mail.ru"}
    )
    assert "не похоже на адрес почты" in answer
    draft = await state.get_draft(PEER)
    assert "recipient_email" not in draft.details


async def test_set_recipient_asks_for_email(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await state.set_draft(
        PEER,
        OrderDraft(items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
                   delivery_method="ozon_pvz", stage="awaiting_confirmation"),
    )

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Иванов Иван", "phone": "79181234567"}
    )
    assert "без неё оплату не выставить" in answer


def test_draft_description_demands_email():
    draft = OrderDraft(
        items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
        delivery_method="ozon_pvz",
        details={"recipient_name": "Иванов", "recipient_phone": "79181234567"},
        stage="awaiting_confirmation",
    )
    described = conversation._describe_draft(draft)
    assert "Почта для чека ещё НЕ записана" in described


def test_receipt_destination_texts():
    assert templates.receipt_destination("a@b.ru", "79181234567") == "a@b.ru"
    assert templates.receipt_destination("", "79181234567") == "номер +7 *** ***-45-67"
    assert templates.receipt_destination("", "") == "указанные вами контакты"
