"""Задача 3.3: вопрос, на который менеджер не ответил."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import worktime
from app.messages import manager as manager_messages, templates
from app.modules.dialog import escalation_log, escalation_state, escalation_watch
from app.modules.dialog.models import Escalation

PEER = 6300


@pytest.fixture
def channels(monkeypatch):
    box: dict = {"client": [], "manager": []}

    async def to_client(peer_id, text, random_id=None):
        box["client"].append(text)

    async def to_manager(text, chat_id=None):
        box["manager"].append(text)

    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    return box


async def open_question(db, *, minutes_ago: int) -> Escalation:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    async with db() as session:
        row = Escalation(
            peer_id=PEER, question="Есть ли опт?", reason="нет данных в ассортименте",
            created_at=created,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.fixture
def always_working(monkeypatch):
    monkeypatch.setattr(worktime, "is_working", lambda moment=None: True)


async def test_reping_after_two_working_hours(clean, channels, always_working):
    question = await open_question(clean, minutes_ago=130)

    result = await escalation_watch.check_open_questions()

    assert result["reping"] == 1 and result["client_told"] == 1
    assert "ждёт ответа" in channels["manager"][-1]
    assert "Есть ли опт?" in channels["manager"][-1]
    assert channels["client"][-1] == templates.escalation_waiting()

    # Отметки поставлены — второй тик молчит.
    async with clean() as session:
        fresh = await session.get(Escalation, question.id)
    assert fresh.reping_sent_at is not None and fresh.client_ping_sent_at is not None

    assert (await escalation_watch.check_open_questions())["reping"] == 0
    assert len(channels["manager"]) == 1 and len(channels["client"]) == 1


async def test_silence_before_the_threshold(clean, channels, always_working):
    await open_question(clean, minutes_ago=60)
    result = await escalation_watch.check_open_questions()
    assert result == {"checked": 1, "reping": 0, "client_told": 0}
    assert channels["manager"] == [] and channels["client"] == []


async def test_night_does_not_count_as_waiting(clean, channels, monkeypatch):
    """Вопрос, заданный ночью, ждёт с открытия, а не всю ночь."""
    monkeypatch.setattr(worktime, "is_working", lambda moment=None: False)
    await open_question(clean, minutes_ago=600)

    result = await escalation_watch.check_open_questions()
    assert result["reping"] == 0
    assert channels["manager"] == []


async def test_answered_question_is_left_alone(clean, channels, always_working):
    question = await open_question(clean, minutes_ago=300)
    await escalation_log.resolve_latest(PEER, admin_id=42)

    result = await escalation_watch.check_open_questions()
    assert result["checked"] == 0
    assert channels["manager"] == []

    async with clean() as session:
        fresh = await session.get(Escalation, question.id)
    assert fresh.resolved_at is not None


async def test_client_ping_can_be_turned_off(clean, channels, always_working, monkeypatch):
    monkeypatch.setattr(escalation_watch.settings, "escalation_client_ping_enabled", False)
    await open_question(clean, minutes_ago=130)

    result = await escalation_watch.check_open_questions()

    assert result["reping"] == 1 and result["client_told"] == 0
    assert channels["manager"], "менеджеру напомнить нужно в любом случае"
    assert channels["client"] == []


async def test_manager_reply_closes_the_question(clean, channels, always_working):
    """Ответ менеджера в диалоге останавливает проверки."""
    from app.modules.dialog import service as dialog_service

    await escalation_state.mark_open(PEER)
    await open_question(clean, minutes_ago=130)

    await dialog_service.handle_message_reply(
        {"peer_id": PEER, "admin_author_id": 7, "text": "Иван, по опту напишу завтра"}
    )

    assert await escalation_state.is_open(PEER) is False
    assert (await escalation_watch.check_open_questions())["checked"] == 0


def test_working_window_is_ten_to_eleven_pm():
    """Менеджеры отвечают все семь дней, с 10:00 до 23:00 по Москве."""
    from datetime import datetime as dt

    def at(hour: int, minute: int = 0, day: int = 27):
        # 27.09.2026 — воскресенье: выходных у менеджера нет.
        return dt(2026, 9, day, hour, minute, tzinfo=worktime.MSK)

    assert worktime.is_working(at(9, 59)) is False
    assert worktime.is_working(at(10)) is True
    assert worktime.is_working(at(22, 59)) is True
    assert worktime.is_working(at(23)) is False

    # Воскресенье считается рабочим днём, как и любой другой.
    assert worktime.working_day_phrase(at(12)) == "сегодня"
    assert worktime.working_day_phrase(at(23, 30)) == "в ближайший рабочий день"

    # Вечер воскресенья и утро понедельника: час до 23:00 плюс час после 10:00.
    assert worktime.working_minutes_between(at(22), at(11, 0, day=28)) == 120
