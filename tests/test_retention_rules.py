"""Повторные касания, задача 0: общий фильтр, пауза, приоритет, журнал воронки."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import client as client_messages, funnel, marketing, templates
from app.messages.models import ClientNotice, ClientPreference, FunnelEvent
from app.modules.dialog import escalation_state, history as dialog_history, vk_client
from app.modules.dialog.models import Conversation, ConversationMessage
from app.modules.orders import retention, state
from app.modules.orders.state import OrderDraft
from tests.test_feedback_repeat_optout import NOW, PEER, TEA, make_order, outbox  # noqa: F401


async def say(db, role: str, at, author=None):
    async with db() as session:
        if await session.get(Conversation, PEER) is None:
            session.add(Conversation(peer_id=PEER))
            await session.flush()
        session.add(ConversationMessage(peer_id=PEER, role=role, content="…", author=author, created_at=at))
        await session.commit()


async def events(db) -> list[tuple[str, dict | None]]:
    async with db() as session:
        rows = (await session.execute(select(FunnelEvent).order_by(FunnelEvent.id))).scalars().all()
    return [(row.event, row.data) for row in rows]


@pytest.mark.parametrize("case, reason", [
    ("opted_out", "отписан"),
    ("unreachable", "не доставляет"),
    ("draft", "черновик"),
    ("in_transit", "не вручён"),
    ("escalation", "вопрос к менеджеру"),
    ("manager_wrote", "менеджер писал"),
    ("client_wrote", "разговор идёт"),
    ("refund", "возврат"),
    ("not_delivered", "не вручён"),
    ("complaint", "жалоба"),
])
async def test_common_filter_blocks(clean, outbox, case, reason):
    fields = {}
    if case == "refund":
        fields = {"status": "refunded"}
    if case == "complaint":
        fields = {"details": {"complaint_at": NOW.isoformat()}}
    order = await make_order(clean, delivered_days_ago=22, **fields)
    if case == "opted_out":
        await marketing.opt_out(PEER)
    if case == "unreachable":
        await marketing.mark_unreachable(PEER)
    if case == "draft":
        await state.set_draft(PEER, OrderDraft(items=TEA, items_total=900, stage="awaiting_delivery"))
    if case == "in_transit":
        await make_order(clean, ozon_posting="0009-1", delivered_at=None, created_at=NOW - timedelta(days=2))
    if case == "escalation":
        await escalation_state.mark_open(PEER)
    if case == "manager_wrote":
        await say(clean, "assistant", NOW - timedelta(hours=47), dialog_history.AUTHOR_MANAGER)
    if case == "client_wrote":
        await say(clean, "user", NOW - timedelta(hours=11))
    if case == "not_delivered":
        # Последний заказ не вручён — по прошлому, вручённому, тоже молчим.
        await make_order(clean, ozon_posting="0009-2", delivered_at=None,
                         not_delivered_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=10))
    result = await retention.check(NOW)
    assert result["sent"] == 0 and outbox.client == []
    blocked = [d for d in result["decisions"] if d.order_id == order.id]
    # Возврат и новый заказ отсекает уже само касание; фильтр всё равно говорит своё.
    assert (blocked[0].reason if blocked else await retention.blocker(PEER, NOW)).find(reason) >= 0


async def test_old_conversation_does_not_block(clean, outbox):
    await make_order(clean, delivered_days_ago=22)
    await say(clean, "user", NOW - timedelta(hours=13))
    await say(clean, "assistant", NOW - timedelta(hours=49), dialog_history.AUTHOR_MANAGER)
    assert (await retention.check(NOW))["sent"] == 1


async def test_gap_defers_until_shelf_then_skips(clean, outbox, monkeypatch):
    monkeypatch.setattr(settings, "marketing_min_gap_days", 3)
    first = await make_order(clean, delivered_days_ago=22)
    # Касание три дня назад — другому заказу того же клиента.
    async with clean() as session:
        session.add(ClientNotice(ref="order:999", event_type=templates.REPEAT_NUDGE, peer_id=PEER,
                                 sent_at=NOW - timedelta(days=2), attempts=1))
        await session.commit()
    result = await retention.check(NOW)
    assert result["sent"] == 0 and "пауза между касаниями до 21.10" in result["decisions"][0].reason
    # Пауза кончилась, срок годности (21 + 7 дней) ещё нет — уходит.
    assert (await retention.check(NOW + timedelta(days=1, hours=1)))["sent"] == 1
    assert await retention.already(first.id, templates.REPEAT_NUDGE)


async def test_gap_longer_than_shelf_skips(clean, outbox, monkeypatch):
    monkeypatch.setattr(settings, "marketing_min_gap_days", 10)
    await make_order(clean, delivered_days_ago=22)
    async with clean() as session:
        session.add(ClientNotice(ref="order:999", event_type=templates.FEEDBACK_ASK, peer_id=PEER,
                                 sent_at=NOW - timedelta(days=1), attempts=1))
        await session.commit()
    # Пауза до NOW+9, а срок годности «Повторить» — до NOW+6: касание пропущено.
    for day in range(0, 12):
        assert (await retention.check(NOW + timedelta(days=day)))["sent"] == 0
    assert outbox.client == []


async def test_unreachable_from_vk_error_and_cleared_by_message(clean, monkeypatch):
    await make_order(clean, delivered_days_ago=22)

    async def refused(peer_id, text, random_id=None, keyboard=None):
        raise vk_client.VkApiError({"error_code": 901, "error_msg": "Can't send messages"})

    monkeypatch.setattr(client_messages.vk_client, "send_message", refused)
    assert (await retention.check(NOW))["sent"] == 0
    async with clean() as session:
        assert (await session.get(ClientPreference, PEER)).unreachable_at is not None
    assert "не доставляет" in await retention.blocker(PEER, NOW)
    await marketing.mark_reachable(PEER)
    assert await retention.blocker(PEER, NOW) is None


async def test_funnel_records_send_press_order_and_optout(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    assert (await retention.check(NOW))["sent"] == 1
    async with clean() as session:
        row = (await session.execute(select(FunnelEvent))).scalars().one()
    assert row.event == "touch:repeat_nudge" and row.order_id == order.id
    # Время касания — «сейчас» проверки, а не часы машины.
    assert abs((row.created_at - NOW).total_seconds()) < 1

    from app.modules.orders import buttons

    await buttons.handle(PEER, {"payload": '{"a":"other","o":%d,"t":"repeat_nudge"}' % order.id})
    await retention.note_order(PEER, 555, NOW + timedelta(days=6))
    await retention.note_opt_out(PEER, NOW + timedelta(days=1))
    await retention.note_order(PEER, 556, NOW + timedelta(days=8))  # позже недели — не касанию
    assert [(e, (d or {}).get("touch")) for e, d in await events(clean)] == [
        ("touch:repeat_nudge", None),
        ("button:other", "repeat_nudge"),
        ("touch_order", "repeat_nudge"),
        ("touch_optout", "repeat_nudge"),
    ]


async def test_record_keeps_event_time(clean):
    await funnel.record(PEER, "touch:x", at=NOW)
    async with clean() as session:
        row = (await session.execute(select(FunnelEvent))).scalars().one()
    assert row.created_at == NOW
