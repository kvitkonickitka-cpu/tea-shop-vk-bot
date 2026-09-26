"""Телефон к одному виду, почта — живая: чек уходит только письмом."""

from __future__ import annotations

import pytest

from app.modules.orders import contacts, conversation, state
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service

PEER = 9800


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("89214477622", "+79214477622"),
        ("+7 (921) 447-76-22", "+79214477622"),
        ("7 921 447 76 22", "+79214477622"),
        ("9214477622", "+79214477622"),
        ("8-861-255-00-00", "+78612550000"),
        ("123", None),
        ("+380 44 123 45 67", None),
        ("", None),
    ],
)
def test_phone_is_normalized(raw, expected):
    assert contacts.normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["ivan.mail.ru", "ivan@@mail.ru", "ivan@mail", "иван@mail.ru", "ivan@mail.123",
     "ivan..petrov@mail.ru", ".ivan@mail.ru", "ivan@-mail.ru", "ivan @mail.ru"],
)
async def test_malformed_email_is_rejected(raw):
    checked = await contacts.check_email(raw)
    assert not checked.ok and "не похоже на адрес" in checked.problem


async def test_domain_without_mail_server_gets_a_suggestion():
    checked = await contacts.check_email("kvitko@yandex.ry")
    assert not checked.ok
    assert "не принимает почту" in checked.problem
    assert checked.suggestion == "kvitko@yandex.ru"

    assert (await contacts.check_email("ivan@gmial.com")).suggestion == "ivan@gmail.com"
    # Для выдуманного домена подсказывать нечего.
    assert (await contacts.check_email("test@test.ru")).suggestion == ""


async def test_good_email_is_cleaned_up():
    checked = await contacts.check_email("  Kvitko.N@Yandex.RU. ")
    assert checked.ok and checked.email == "Kvitko.N@yandex.ru"
    assert (await contacts.check_email("почта@мойсайт.рф")).ok is False  # ящик кириллицей
    assert (await contacts.check_email("mail@мойсайт.рф")).ok is True


async def test_dns_silence_does_not_block_the_client(monkeypatch):
    async def silent(domain):
        return None

    monkeypatch.setattr(contacts, "_has_mail_server", silent)
    assert (await contacts.check_email("ivan@gmial.com")).ok is True


async def _draft():
    await state.set_draft(
        PEER,
        OrderDraft(items=[{"name": "Чай", "quantity": 1, "price": 100}], items_total=100.0,
                   delivery_method="ozon_pvz", stage="awaiting_confirmation"),
    )


async def test_set_recipient_stores_plus7_and_checked_email(clean, monkeypatch):
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await _draft()

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Квитко Никита", "phone": "89214477622", "email": "kvitko@yandex.ru"}
    )

    details = (await state.get_draft(PEER)).details
    assert details["recipient_phone"] == "+79214477622"
    assert details["recipient_email"] == "kvitko@yandex.ru"
    assert "+79214477622" in answer


async def test_set_recipient_refuses_a_dead_mail_domain(clean, monkeypatch):
    """26.09.2026: test@test.ru записался — чек ушёл бы в никуда."""
    monkeypatch.setattr(payment_service, "is_enabled", lambda: True)
    await _draft()

    answer = await conversation._execute_set_recipient(
        PEER, {"name": "Квитко Никита", "phone": "89214477622", "email": "test@test.ru"}
    )

    assert "Почта не записана" in answer and "не принимает почту" in answer
    assert "recipient_email" not in (await state.get_draft(PEER)).details

    typo = await conversation._execute_set_recipient(
        PEER, {"name": "Квитко Никита", "phone": "89214477622", "email": "k@yandex.ry"}
    )
    assert "k@yandex.ru" in typo and "спроси" in typo
