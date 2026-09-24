"""Задача 3.2: уведомления менеджеру не теряются."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.messages import manager as manager_messages
from app.messages.models import ManagerNotification


@pytest.fixture
def telegram(monkeypatch):
    """Телеграм, который можно заставить отказать."""
    box: dict = {"sent": [], "fail": False}

    async def fake_send(text, chat_id=None):
        if box["fail"]:
            raise RuntimeError("Telegram timeout")
        box["sent"].append({"text": text, "chat_id": chat_id})

    monkeypatch.setattr(manager_messages.telegram_client, "send_message", fake_send)
    return box


async def rows(db) -> list[ManagerNotification]:
    async with db() as session:
        return list(
            (await session.execute(select(ManagerNotification).order_by(ManagerNotification.id)))
            .scalars()
            .all()
        )


async def test_notification_is_stored_then_sent(clean, telegram):
    stored = await manager_messages.notify(
        manager_messages.ESCALATION, "Вопрос клиента", peer_id=10, chat_id="-100"
    )
    assert stored is True
    assert telegram["sent"] == [{"text": "Вопрос клиента", "chat_id": "-100"}]

    saved = await rows(clean)
    assert len(saved) == 1
    assert saved[0].sent_at is not None and saved[0].attempts == 1


async def test_failed_send_stays_in_the_queue(clean, telegram):
    telegram["fail"] = True
    stored = await manager_messages.notify(
        manager_messages.ESCALATION, "Вопрос клиента", peer_id=10
    )
    # Сохранено — значит обещание клиенту можно давать.
    assert stored is True

    saved = await rows(clean)
    assert saved[0].sent_at is None
    assert saved[0].attempts == 1
    assert "Telegram timeout" in saved[0].last_error
    assert saved[0].next_attempt_at is not None

    # Телеграм ожил — следующий тик дошлёт.
    telegram["fail"] = False
    async with clean() as session:
        row = await session.get(ManagerNotification, saved[0].id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.commit()

    result = await manager_messages.flush()
    assert result == {"tried": 1, "sent": 1, "failed": 0}
    assert telegram["sent"][-1]["text"] == "Вопрос клиента"

    saved = await rows(clean)
    assert saved[0].sent_at is not None


async def test_backoff_holds_the_next_try(clean, telegram):
    telegram["fail"] = True
    await manager_messages.notify(manager_messages.ORDER_CARD, "Карточка", order_id=5)

    # Пауза ещё не прошла — очередь его не берёт.
    result = await manager_messages.flush()
    assert result["tried"] == 0

    assert manager_messages._backoff(1) < manager_messages._backoff(4)
    assert manager_messages._backoff(50) == manager_messages._BACKOFF_CAP


async def test_gives_up_after_ten_attempts(clean, telegram, caplog):
    telegram["fail"] = True
    await manager_messages.notify(manager_messages.ESCALATION, "Вопрос", peer_id=11)

    saved = await rows(clean)
    notification_id = saved[0].id

    for attempt in range(2, manager_messages.MAX_ATTEMPTS + 1):
        async with clean() as session:
            row = await session.get(ManagerNotification, notification_id)
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
        await manager_messages.flush()

    saved = await rows(clean)
    assert saved[0].attempts == manager_messages.MAX_ATTEMPTS

    # Больше не берём: своими силами не доставим.
    async with clean() as session:
        row = await session.get(ManagerNotification, notification_id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.commit()
    assert (await manager_messages.flush())["tried"] == 0

    # И такое попадает в отдельный блок отчёта.
    undelivered = await manager_messages.undelivered()
    assert [row.id for row in undelivered] == [notification_id]


async def test_order_card_goes_through_the_queue(clean, telegram):
    """Карточки заказов тоже в очереди, а не в одной попытке."""
    import types

    from app.modules.orders import order_chat

    order = types.SimpleNamespace(id=77, peer_id=12)
    telegram["fail"] = True
    assert await order_chat.send(order, "💰 Оплачено") is True

    saved = await rows(clean)
    assert saved[0].kind == manager_messages.ORDER_CARD
    assert saved[0].order_id == 77 and saved[0].payload == "💰 Оплачено"


async def test_undelivered_report_is_sent_once_a_day(clean, telegram, monkeypatch):
    from app.modules.reports import service as reports_service

    reported: list[str] = []

    async def fake_send(text, chat_id=None):
        reported.append(text)

    monkeypatch.setattr(reports_service.telegram_client, "send_message", fake_send)
    monkeypatch.setattr(reports_service.settings, "telegram_reports_chat_id", "-200")

    # Ничего не потеряно — отчёта нет.
    assert (await reports_service.report_undelivered())["undelivered"] == 0
    assert reported == []

    async with clean() as session:
        session.add(
            ManagerNotification(
                kind=manager_messages.ESCALATION, order_id=3, peer_id=13,
                payload="Вопрос", attempts=manager_messages.MAX_ATTEMPTS,
                last_error="Telegram timeout",
            )
        )
        await session.commit()

    first = await reports_service.report_undelivered()
    assert first["sent"] is True
    assert "Не доставлено менеджеру: 1" in reported[0]
    assert "Telegram timeout" in reported[0]

    # Второй раз в сутки не повторяем.
    second = await reports_service.report_undelivered()
    assert second["sent"] is False and len(reported) == 1


async def test_admin_gets_a_vk_message_when_telegram_gives_up(clean, telegram, monkeypatch):
    """Пункт 2 ревью: второй канал для того, что не дошло в телеграм."""
    to_admin: list[dict] = []

    async def to_vk(peer_id, text, random_id=None):
        to_admin.append({"peer_id": peer_id, "text": text, "random_id": random_id})

    monkeypatch.setattr(manager_messages.vk_client, "send_message", to_vk)
    monkeypatch.setattr(manager_messages.settings, "admin_vk_id", 777001)

    telegram["fail"] = True
    await manager_messages.notify(
        manager_messages.ESCALATION, "<b>Вопрос клиента</b>\nЕсть ли опт?", order_id=5, peer_id=9
    )

    saved = await rows(clean)
    notification_id = saved[0].id

    # Пока попытки не исчерпаны, администратора не трогаем.
    assert to_admin == []

    for _ in range(manager_messages.MAX_ATTEMPTS - 1):
        async with clean() as session:
            row = await session.get(ManagerNotification, notification_id)
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
        await manager_messages.flush()

    assert len(to_admin) == 1, to_admin
    assert to_admin[0]["peer_id"] == 777001
    assert "не доставлено в телеграм" in to_admin[0]["text"]
    assert "заказ №5" in to_admin[0]["text"]
    # Разметку телеграма в ВК не показываем.
    assert "<b>" not in to_admin[0]["text"] and "Вопрос клиента" in to_admin[0]["text"]

    # Отметка стоит — второй раз администратора не будим.
    async with clean() as session:
        row = await session.get(ManagerNotification, notification_id)
    assert row.fallback_sent_at is not None

    async with clean() as session:
        row = await session.get(ManagerNotification, notification_id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        row.attempts = manager_messages.MAX_ATTEMPTS - 1
        await session.commit()
    await manager_messages.flush()
    assert len(to_admin) == 1


async def test_without_admin_id_we_at_least_shout_in_the_log(clean, telegram, monkeypatch, caplog):
    monkeypatch.setattr(manager_messages.settings, "admin_vk_id", 0)
    telegram["fail"] = True
    await manager_messages.notify(manager_messages.ORDER_CARD, "Карточка", order_id=6)

    saved = await rows(clean)
    for _ in range(manager_messages.MAX_ATTEMPTS - 1):
        async with clean() as session:
            row = await session.get(ManagerNotification, saved[0].id)
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
        await manager_messages.flush()

    assert any("ADMIN_VK_ID" in record.message for record in caplog.records)
