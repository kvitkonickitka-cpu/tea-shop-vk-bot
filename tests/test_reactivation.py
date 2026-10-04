"""Повторные касания, задача 5: реактивация через 90 дней."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.messages import templates
from app.messages.models import ClientNotice
from app.modules.catalog import service as catalog_service, sheet
from app.modules.orders import buttons, retention
from tests.test_feedback_ask import labels, outbox  # noqa: F401
from tests.test_feedback_repeat_optout import NOW, PEER, make_order
from tests.test_second_touch import CATALOG


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    items = [dict(item) for item in CATALOG]
    monkeypatch.setattr(catalog_service, "load_items", lambda: items)
    return items


def test_sheet_novelty_column():
    parsed = sheet.parse_csv("Название,Цена,Новинка\nГаба,1100,да\nШу Пуэр,900,\nБай Ча,800,может\n")
    assert [item["is_new"] for item in parsed.items] == [True, False, False]
    assert parsed.errors == [] and "«Новинка» — «может»" in parsed.warnings[0]


async def test_novelties_after_ninety_days(clean, outbox, catalog):
    catalog[1]["is_new"] = True
    catalog[2]["is_new"] = True
    order = await make_order(clean, delivered_days_ago=90)
    result = await retention.check(NOW, kinds={templates.REACTIVATION})
    assert result["sent"] == 1
    assert outbox.client[-1] == (
        "Здравствуйте! Давно не виделись 🍵 У нас появилось новое:\n"
        "• Да Хун Пао (1500 ₽) — Утёсный улун с жареными нотами\n"
        "• Габа (1100 ₽) — Мягкий и сладкий\n"
        "Если захотите — нажмите «Взять», повторите прошлый заказ или попросите подобрать чай.\n"
        "Если не хотите таких сообщений — напишите «стоп»."
    )
    assert labels(outbox.boards[-1]) == [
        "Взять Да Хун Пао", "Взять Габа", "Повторить прошлый заказ", "Подобрать чай"]
    advise = outbox.boards[-1]["buttons"][1][1]["action"]
    assert (await buttons.handle(PEER, {"payload": advise["payload"]})).to_model
    assert json.loads(advise["payload"]) == {"a": "advise", "t": "reactivation"}
    # После реактивации до нового заказа — никаких других касаний.
    assert await retention.blocker(PEER, NOW + timedelta(days=10)) == "после реактивации ждём нового заказа"
    assert await retention.already(order.id, templates.REACTIVATION)


async def test_without_novelties_by_recommendations(clean, outbox):
    await make_order(clean, delivered_days_ago=95)
    await retention.check(NOW, kinds={templates.REACTIVATION})
    assert outbox.client[-1].startswith(
        "Здравствуйте! Давно не виделись 🍵 К Те Гуань Инь 100 г у нас советуют:\n• Да Хун Пао"
    )


async def test_not_too_early_not_too_late_not_too_often(clean, outbox):
    await make_order(clean, delivered_days_ago=89)
    assert (await retention.check(NOW, kinds={templates.REACTIVATION}))["sent"] == 0
    await make_order(clean, peer_id=PEER + 1, delivered_days_ago=121, ozon_posting="0002-1")
    assert (await retention.check(NOW, kinds={templates.REACTIVATION}))["sent"] == 0
    await make_order(clean, peer_id=PEER + 2, delivered_days_ago=100, ozon_posting="0003-1")
    async with clean() as session:
        session.add(ClientNotice(ref="order:777", event_type=templates.REACTIVATION, peer_id=PEER + 2,
                                 sent_at=NOW - timedelta(days=170), attempts=1))
        await session.commit()
    assert (await retention.check(NOW, kinds={templates.REACTIVATION}))["sent"] == 0


async def test_new_order_since_cancels_reactivation(clean, outbox):
    old = await make_order(clean, delivered_days_ago=95)
    await make_order(clean, delivered_days_ago=40, ozon_posting="0004-1",
                     created_at=old.delivered_at + timedelta(days=20))
    assert (await retention.check(NOW, kinds={templates.REACTIVATION}))["sent"] == 0
