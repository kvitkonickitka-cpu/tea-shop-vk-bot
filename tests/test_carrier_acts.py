"""Акты перевозчиков (A13): разбор CSV, запись по треку, тревога об отклонении от котировки."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import settings
from app.messages import manager as manager_messages
from app.modules.delivery import acts
from app.modules.orders import delivery_watch
from tests.test_fin_model import _order, fin  # noqa: F401


def test_parse_act_csv():
    rows, problems = acts.parse(
        "Трек;Сумма;Вид;Дата;Номер\n10001;230,50;доставка;30.11.2026;А-1\n10002;150;возврат;;\n"
        "10003;;;\n;100\n10004;12;обмен\n".encode("utf-8-sig"))
    assert [(r.track, r.amount, r.kind, r.act_date, r.act_number) for r in rows] == [
        ("10001", Decimal("230.50"), "доставка", date(2026, 11, 30), "А-1"),
        ("10002", Decimal("150"), "возврат", None, None)]
    assert problems == ["строка 4: нет суммы", "строка 5: нет трека или сумма меньше нуля",
                        "строка 6: вид — «доставка» или «возврат»"]
    rows, _ = acts.parse(b"20001,99.9\n")
    assert rows[0].amount == Decimal("99.9")


def test_cdek_actual_cost_is_read_from_order_info():
    observed = delivery_watch.read_cdek({"entity": {"statuses": [], "delivery_detail": {"total_sum": 312.4}}})
    assert observed.carrier_cost == 312.4
    assert delivery_watch.read_cdek({"entity": {"statuses": []}}).carrier_cost is None


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def notify(kind, text_, **kwargs):
        sent.append(text_)
        return True

    monkeypatch.setattr(manager_messages, "notify", notify)
    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")
    return sent


async def test_acts_are_stored_by_track_and_deviation_is_reported_once(fin, db, alerts):
    order = await _order(db, 9701, [{"name": "Те Гуань Инь 100 г", "quantity": 2, "price": 390}], delivery=200, carrier=207.0)
    await fin.execute("update orders set ozon_posting = '0123-0001-1' where id = $1", order)
    rows, _ = acts.parse(b"0123-0001-1;260\n9999;100\n")
    result = await acts.store(rows)
    assert result == {"записано": 1, "трек не найден": ["9999"]}
    assert await fin.fetchval("select carrier || ':' || entered_by from fin.carrier_acts") == "ozon:бот"
    first = await acts.check()
    # 260 к котировке 200 — +30 %: по заказу и за месяц.
    assert first["отправлено"] == 2 and "+30 %" in alerts[0] and "Ozon" in alerts[0]
    assert (await acts.check())["отправлено"] == 0


async def test_store_without_fin_schema_says_so(clean):
    with pytest.raises(ValueError, match="006"):
        await acts.store([acts.ActRow("1", Decimal(1))])
    assert (await acts.check())["skipped"]
