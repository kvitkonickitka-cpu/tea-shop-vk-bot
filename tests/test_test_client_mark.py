"""Карточки менеджеру и отчёт о диалоге помечают тестового клиента."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.core.config import settings
from app.messages import manager as manager_messages
from app.modules.analytics import service as analytics
from app.modules.orders.models import Order
from app.modules.reports import service as reports

TESTER, REAL = 362545925, 7701


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def to_telegram(text, chat_id=None):
        box.append(text)

    monkeypatch.setattr(settings, "test_vk_ids", str(TESTER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_telegram)
    return box


async def test_cards_of_test_accounts_are_marked(clean, sent):
    await manager_messages.notify("question", "❓ Вопрос клиента", peer_id=TESTER)
    await manager_messages.notify("question", "❓ Вопрос клиента", peer_id=REAL)
    assert sent[0] == f"{analytics.TEST_MARK}\n❓ Вопрос клиента"
    assert sent[1] == "❓ Вопрос клиента"


async def test_test_order_is_marked_even_for_a_real_account(clean, sent):
    async with clean() as session:
        order = Order(peer_id=REAL, items=[], items_total=0, total=0, is_test=True)
        session.add(order)
        await session.commit()
    await manager_messages.notify("paid", "💰 Оплачено", order_id=order.id, peer_id=REAL)
    assert sent[-1].startswith(analytics.TEST_MARK)


def test_dialog_report_shows_the_mark():
    text = reports._build_telegram_message(NS(peer_id=TESTER), [1, 2], "Тема: …", test=True)
    assert text.startswith(f"{analytics.TEST_MARK}\n<b>Диалог завершён</b>")
    assert reports._build_telegram_message(NS(peer_id=REAL), [1], "Тема: …").startswith("<b>Диалог завершён</b>")
