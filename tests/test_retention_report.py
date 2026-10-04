"""Повторные касания в ежедневном отчёте и статистика «вручено»."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.messages import funnel
from app.modules.ops import report
from app.modules.orders import retention
from tests.test_feedback_repeat_optout import NOW, PEER, make_order


async def test_stats_by_touch(clean):
    await funnel.record(PEER, "touch:repeat_nudge", at=NOW - timedelta(hours=2))
    await funnel.record(PEER, "button:repeat", touch="repeat_nudge", at=NOW - timedelta(hours=1))
    await funnel.record(PEER, "touch_order", touch="repeat_nudge", at=NOW - timedelta(hours=1))
    await funnel.record(PEER, "touch:feedback_ask", at=NOW - timedelta(days=5))
    await funnel.record(PEER, "touch_optout", touch="feedback_ask", at=NOW - timedelta(days=4))
    await funnel.record(PEER, "button:pt", at=NOW - timedelta(hours=1))  # не касание
    day = await retention.stats(NOW - timedelta(days=1))
    month = await retention.stats(NOW - timedelta(days=30))
    assert day["repeat_nudge"] == {"sent": 1, "pressed": 1, "orders": 1, "optouts": 0}
    assert day["feedback_ask"]["sent"] == 0
    assert month["feedback_ask"] == {"sent": 1, "pressed": 0, "orders": 0, "optouts": 1}
    lines = report._retention_lines({"day": day, "month": month, "share": (4, 1)})
    assert "«Повторить заказ?»: 1/1 · 1/1 · 1/1 · 0/0" in lines
    assert "оценка: 0/1 · 0/0 · 0/0 · 0/1" in lines
    assert lines[-1] == "Повторная покупка в течение 60 дней после первой: 25 % (1 из 4)"


async def test_repeat_share(clean):
    # Первый купил 100 дней назад и снова через 30; второй — только раз; третий — недавно.
    await make_order(clean, created_at=NOW - timedelta(days=100), ozon_posting="1")
    await make_order(clean, created_at=NOW - timedelta(days=70), ozon_posting="2")
    await make_order(clean, peer_id=PEER + 1, created_at=NOW - timedelta(days=90), ozon_posting="3")
    await make_order(clean, peer_id=PEER + 2, created_at=NOW - timedelta(days=10), ozon_posting="4")
    assert await retention.repeat_share(NOW) == (2, 1)


async def test_delivery_stats(clean):
    await make_order(clean, ozon_posting="o-1")
    await make_order(clean, ozon_posting=None, cdek_uuid="c-1", delivered_at=None,
                     handed_over_at=datetime.now(timezone.utc) - timedelta(days=20))  # счёт от часов базы
    stats = await retention.delivery_stats()
    assert stats["Ozon"]["delivered"] == 1
    assert stats["СДЭК"]["delivered"] == 0 and stats["СДЭК"]["stuck_14d"] == 1
