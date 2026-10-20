"""Регламенты Ops (пакет B): контроли, утренний список, закрытие месяца, налоговый календарь."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core import heartbeat
from app.core.config import settings
from app.messages import manager as manager_messages
from app.modules.ops import commands, controls
from app.modules.orders.models import Order
from tests.test_fin_model import fin  # noqa: F401

NOW = datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc)  # 10:30 МСК


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def notify(kind, text_, **kwargs):
        messages.append((kind, text_))
        return True

    monkeypatch.setattr(manager_messages, "notify", notify)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example")
    monkeypatch.setattr(settings, "internal_api_token", "t0ken")
    monkeypatch.setattr(settings, "settlement_receipt_enabled", True)
    monkeypatch.setattr(settings, "ops_admin_tg_ids", "")
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "")
    return messages


async def order(db, **fields) -> int:
    values = dict(peer_id=8801, items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 900}], items_total=900,
                  delivery_cost=0, total=900, delivery_method="ozon_pvz", status="confirmed", payment_status="succeeded",
                  details={}, paid_at=NOW - timedelta(hours=2), created_at=NOW - timedelta(hours=2))
    values.update(fields)
    async with db() as session:
        row = Order(**values)
        session.add(row)
        await session.commit()
        return row.id


async def test_critical_controls_alert_once_and_summary_daily(clean, sent, monkeypatch):
    late = await order(clean, paid_at=NOW - timedelta(hours=30))
    delivered = await order(clean, paid_at=NOW - timedelta(days=4), handed_over_at=NOW - timedelta(days=3),
                            delivered_at=NOW - timedelta(hours=5))
    await order(clean, paid_at=NOW - timedelta(days=12), handed_over_at=NOW - timedelta(days=11))
    await order(clean, paid_at=NOW - timedelta(days=40), is_test=True)  # тестовый — не считаем
    result = await controls.check_controls(now=NOW)
    alerts = [t for k, t in sent if "🚨 <b>Заказ" in t]
    assert len(alerts) == 2
    assert any(f"№{late}" in a and "не передано" in a for a in alerts)
    assert any(f"№{delivered}" in a and "закрывающего чека" in a for a in alerts)
    summary = [t for k, t in sent if t.startswith("<b>Контроли")][0]
    assert "❌ Оплачено больше 10 дней назад и не вручено: 1" in summary
    assert "⚪ Код ЧЗ в чеке не введён в оборот" in summary
    assert result["сводка"]
    sent.clear()

    async def ran_today(name):
        return NOW

    monkeypatch.setattr(heartbeat, "last_run", ran_today)
    again = await controls.check_controls(now=NOW + timedelta(minutes=5))
    assert again["тревог"] == 0 and "сводка" not in again and sent == []


async def test_summary_waits_for_ten(clean, sent):
    early = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)  # 08:00 МСК
    assert "сводка" not in await controls.check_controls(now=early)
    assert sent == []


async def test_morning_list_blocks_and_empty_day(clean, sent, monkeypatch):
    text_ = await controls.render_morning(NOW)
    assert text_.endswith("Дел нет.")
    to_pack = await order(clean)
    to_hand = await order(clean, packed_at=NOW - timedelta(hours=1), delivery_method="cdek_pvz")
    ozon = await order(clean, packed_at=NOW - timedelta(hours=1), ozon_posting="0001-1")
    pickup = await order(clean, handed_over_at=NOW - timedelta(days=3), at_pickup_at=NOW - timedelta(days=1),
                         storage_until=date(2026, 11, 25))
    async with clean() as session:
        await session.execute(text(
            "insert into escalations (peer_id, question, reason, created_at) values"
            " (5, 'q', 'r', :day), (6, 'q', 'r', :night)"),
            {"day": NOW - timedelta(hours=1), "night": datetime(2026, 11, 19, 21, 30, tzinfo=timezone.utc)})
        await session.commit()
    text_ = await controls.render_morning(NOW)
    assert f"• №{to_pack} — <a href=\"https://bot.example/pack/" in text_
    assert f"• №{to_hand} — СДЭК" in text_ and f"• №{ozon} — Ozon" in text_
    assert "Подтвердить в Ozon" in text_ and f"№{pickup} — хранится до 25.11" in text_
    answer = text_.split("💬 <b>Ответить</b>")[1]
    # Ночной вопрос — первым.
    assert answer.index("sel=6") < answer.index("sel=5") and "🌙" in answer
    result = await controls.morning_list(now=NOW)
    assert result["sent"] and sent[-1][0] == manager_messages.MORNING_LIST

    async def ran_today(name):
        return NOW

    monkeypatch.setattr(heartbeat, "last_run", ran_today)
    assert (await controls.morning_list(now=NOW))["skipped"]


async def test_month_close_first_and_repeat_on_third(clean, sent, monkeypatch):
    await order(clean, paid_at=datetime(2026, 10, 10, tzinfo=timezone.utc), receipt_status="succeeded")
    await order(clean, paid_at=datetime(2026, 10, 11, tzinfo=timezone.utc), receipt_status="pending")
    first = datetime(2026, 11, 1, 7, 5, tzinfo=timezone.utc)
    result = await controls.month_close(now=first)
    assert result["sent"] and result["месяц"] == "2026-10" and result["❌"] == 1
    text_ = sent[-1][1]
    assert "Закрытие месяца 10.2026" in text_ and "без чека предоплаты 1" in text_ and "☐ Инвентаризация" in text_
    assert (await controls.month_close(now=first + timedelta(hours=1)))["skipped"] == "уже отправлен"
    assert (await controls.month_close(now=datetime(2026, 11, 2, 8, tzinfo=timezone.utc)))["skipped"]
    third = await controls.month_close(now=datetime(2026, 11, 3, 8, tzinfo=timezone.utc))
    assert third["sent"] and "повтор" in sent[-1][1]


async def test_tax_reminders_and_marking(fin, db, sent):
    rows = await controls.tax_list()
    tax_id = [r for r in rows if r[1] == date(2026, 12, 28)][0][0]
    week_before = datetime(2026, 12, 21, 7, 0, tzinfo=timezone.utc)
    assert (await controls.tax_reminders(now=week_before))["напоминаний"] == 1
    assert "Через неделю, 28.12" in sent[-1][1] and f"/tax {tax_id} оплачено" in sent[-1][1]
    assert (await controls.tax_reminders(now=week_before))["напоминаний"] == 0
    reply = await commands.run("tax", [str(tax_id), "оплачено", "18014,08"], {"id": 1, "username": "nikita"})
    assert "оплачено, 18014.08 ₽" in reply
    assert str(await fin.fetchval("select paid_amount from fin.tax_calendar where id = $1", tax_id)) == "18014.08"
    assert await fin.fetchval("select status from fin.tax_calendar where id = $1", tax_id) == "оплачено"
    assert (await controls.tax_reminders(now=datetime(2026, 12, 27, 7, 0, tzinfo=timezone.utc)))["напоминаний"] == 0
    assert "нужна сумма" in await commands.run("tax", [str(tax_id), "оплачено"], {"id": 1})
    listing = await commands.run("tax", [], {"id": 1})
    assert "Налоговый календарь" in listing and "Декларация УСН" in listing


async def test_ads_reminder_during_campaign(fin, db, sent):
    await fin.execute("insert into fin.inputs (key, value_text, entered_by) values ('ad_campaign_start', '2026-11-15', 'Никита')")
    noon = datetime(2026, 11, 20, 8, 30, tzinfo=timezone.utc)  # 11:30 МСК
    assert (await controls.ads_reminder(now=noon))["sent"]
    assert "Нет расхода рекламы за 19.11" in sent[-1][1]
    assert (await controls.ads_reminder(now=noon))["skipped"] == "уже напоминали"
    await fin.execute("insert into fin.ad_spend (date, spend_own, entered_by) values ('2026-11-20', 500, 'Илья')")
    assert (await controls.ads_reminder(now=noon + timedelta(days=1)))["ok"]
    before = datetime(2026, 11, 10, 8, 30, tzinfo=timezone.utc)
    assert (await controls.ads_reminder(now=before))["skipped"] == "кампания не идёт"


async def test_heartbeats_mark_daily_jobs(clean, sent):
    await controls.check_controls(now=NOW)
    assert await heartbeat.last_run("контроли: сводка") is not None
