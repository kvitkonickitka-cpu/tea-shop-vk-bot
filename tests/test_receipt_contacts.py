"""Задача 4: почта необязательна, чек уходит на телефон."""

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


def test_receipt_takes_email_or_phone():
    with_email = yookassa_client.receipt_customer(
        full_name="Иванов Иван", email="a@b.ru", phone="79181234567"
    )
    assert with_email == {"full_name": "Иванов Иван", "email": "a@b.ru"}

    without_email = yookassa_client.receipt_customer(
        full_name="Иванов Иван", email="", phone="8 918 123-45-67"
    )
    assert without_email == {"full_name": "Иванов Иван", "phone": "79181234567"}


async def test_payment_without_any_contact_is_refused(monkeypatch):
    """Совсем без контактов чек не выписать — и мы говорим это сами."""
    async def must_not_call(*args, **kwargs):
        raise AssertionError("пошли в ЮKassa с пустым чеком")

    monkeypatch.setattr(yookassa_client, "_call", must_not_call)

    with pytest.raises(yookassa_client.YooKassaError) as error:
        await yookassa_client.create_payment(
            order_key="vk1-1", items=[{"name": "Чай", "quantity": 1, "price": 800}],
            delivery_cost=0, delivery_label="", email="", phone="", full_name="Иванов",
            description="Заказ",
        )
    assert "ни почты, ни телефона" in str(error.value)


async def test_confirm_order_goes_through_without_email(clean, monkeypatch):
    """Главное изменение задачи: без почты заказ оформляется."""
    created: dict = {}

    async def fake_create(draft, order_key):
        created["order_key"] = order_key
        created["email"] = draft.details.get("recipient_email", "")
        created["phone"] = draft.details.get("recipient_phone", "")
        return yookassa_client.Payment(
            id="pay-x", status="pending", paid=False,
            confirmation_url="https://yoomoney.ru/checkout/pay/x",
            receipt_registration="pending", test=True, amount=917.0,
        )

    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    monkeypatch.setattr(payment_service, "create_payment", fake_create)

    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800.0, delivery_cost=117.0, delivery_method="ozon_pvz",
        delivery_label="Ozon, пункт выдачи: Ставропольская 230",
        details={"recipient_name": "Иванов Иван", "recipient_phone": "+7 918 123-45-67",
                 "ozon_point_id": 42},
        stage="awaiting_confirmation",
    )
    await state.set_draft(PEER, draft)

    result = await conversation._execute_confirm_order(PEER)

    assert "Оплатить: https://yoomoney.ru" in result.client_reply
    # Клиенту сказали, куда придёт чек, и номер под маской.
    assert "+7 *** ***-45-67" in result.client_reply
    assert created["email"] == "" and created["phone"] == "+7 918 123-45-67"


async def test_confirm_order_asks_for_a_whole_phone(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)

    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 800}],
        items_total=800.0, delivery_cost=117.0, delivery_method="ozon_pvz",
        details={"recipient_name": "Иванов Иван", "recipient_phone": "918-12-34",
                 "ozon_point_id": 42},
        stage="awaiting_confirmation",
    )
    await state.set_draft(PEER, draft)

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
        PEER, {"name": "Иванов Иван", "phone": "123"}
    )
    assert "не похож на настоящий" in answer
    draft = await state.get_draft(PEER)
    assert "recipient_phone" not in draft.details


async def test_set_recipient_asks_about_email_once(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await state.set_draft(
        PEER,
        OrderDraft(items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
                   delivery_method="ozon_pvz", stage="awaiting_confirmation"),
    )

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Иванов Иван", "phone": "79181234567"}
    )
    # Почта не блокирует: инструмент разрешает подтверждать заказ.
    assert "достаточно для оформления" in answer
    assert "confirm_order" in answer
    assert "без неё оплату не выставить" not in answer


def test_draft_description_does_not_demand_email():
    draft = OrderDraft(
        items=[{"name": "Чай", "quantity": 1, "price": 800}], items_total=800.0,
        delivery_method="ozon_pvz",
        details={"recipient_name": "Иванов", "recipient_phone": "79181234567"},
        stage="awaiting_confirmation",
    )
    described = conversation._describe_draft(draft)
    assert "чек уйдёт по номеру телефона" in described
    assert "НЕ записана" not in described


def test_receipt_destination_texts():
    assert templates.receipt_destination("a@b.ru", "79181234567") == "a@b.ru"
    assert templates.receipt_destination("", "79181234567") == "номер +7 *** ***-45-67"
    assert templates.receipt_destination("", "") == "указанные вами контакты"
