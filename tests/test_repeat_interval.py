"""Повторные касания, задача 3: личный интервал «Повторить» и пропуск после «Не моё»."""

from __future__ import annotations

from datetime import timedelta

from app.core.database import get_session_factory
from app.messages import templates
from app.modules.orders import feedback, repeat_nudge, retention
from tests.test_feedback_repeat_optout import NOW, PEER, make_order, outbox  # noqa: F401


async def history(db, gaps_days: list[int], *, last_delivered_days_ago: float, peer_id: int = PEER):
    """Заказы клиента с заданными промежутками; последний вручён N дней назад."""
    last_created = NOW - timedelta(days=last_delivered_days_ago + 4)
    moments = [last_created]
    for gap in reversed(gaps_days):
        moments.insert(0, moments[0] - timedelta(days=gap))
    orders = []
    for number, created in enumerate(moments):
        orders.append(await make_order(
            db, peer_id=peer_id, ozon_posting=f"{peer_id}-{number}", created_at=created,
            delivered_at=created + timedelta(days=4), handed_over_at=created + timedelta(days=1),
        ))
    return orders


async def interval(order):
    async with get_session_factory()() as session:
        return await repeat_nudge.personal_interval(session, order)


async def test_one_order_keeps_packs_rule(clean):
    order = await make_order(clean, delivered_days_ago=10)
    assert await interval(order) is None


async def test_median_of_gaps(clean):
    orders = await history(clean, [30, 40, 20], last_delivered_days_ago=5)
    assert await interval(orders[-1]) == timedelta(days=30)
    orders2 = await history(clean, [30, 45], last_delivered_days_ago=5, peer_id=PEER + 1)  # чётное — среднее двух
    assert await interval(orders2[-1]) == timedelta(days=37, hours=12)


async def test_median_is_clamped(clean):
    fast = await history(clean, [5, 7], last_delivered_days_ago=1)
    assert await interval(fast[-1]) == timedelta(days=14)
    slow = await history(clean, [120, 90], last_delivered_days_ago=1, peer_id=PEER + 1)
    assert await interval(slow[-1]) == timedelta(days=60)


async def test_monthly_client_gets_repeat_at_his_rhythm(clean, outbox):
    # Раз в 30 дней. Последний заказ сделан 30 дней назад и вручён 26 дней назад.
    orders = await history(clean, [30, 30], last_delivered_days_ago=26)
    last = orders[-1]
    # Правило пачек (21 день) уже прошло бы пять дней назад — но по ритму рано.
    assert (await retention.check(NOW - timedelta(days=1)))["sent"] == 0
    assert (await retention.check(NOW))["sent"] == 1
    assert await retention.already(last.id, templates.REPEAT_NUDGE)


async def test_not_mine_skips_repeat(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await feedback.rate(PEER, order.id, "no", "button")
    result = await retention.check(NOW, kinds={templates.REPEAT_NUDGE})
    assert result["sent"] == 0 and result["due"] == 0
