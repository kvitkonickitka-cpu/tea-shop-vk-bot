"""Повторные касания, задача 4: второй шанс — другой сорт."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.messages import templates
from app.modules.catalog import service as catalog_service
from app.modules.dialog import history as dialog_history
from app.modules.orders import buttons, feedback, retention, second_touch, state
from tests.test_feedback_ask import labels, outbox  # noqa: F401
from tests.test_feedback_repeat_optout import NOW, PEER, make_order
from tests.test_retention_rules import say

CATALOG = [
    {"name": "Те Гуань Инь 100 г", "price": 900, "in_stock": True, "recommended": ["Да Хун Пао", "Габа"],
     "description": "Свежий цветочный улун. Хорош днём."},
    {"name": "Да Хун Пао", "price": 1500, "in_stock": True, "recommended": [],
     "description": "Утёсный улун с жареными нотами. Для вечера."},
    {"name": "Габа", "price": 1100, "in_stock": True, "recommended": [], "description": "Мягкий и сладкий."},
]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    items = [dict(item) for item in CATALOG]
    monkeypatch.setattr(catalog_service, "load_items", lambda: items)
    return items


async def repeat_sent(db, order, at):
    """«Повторить заказ?» по заказу ушло в момент `at`."""
    result = await retention.check(at, kinds={templates.REPEAT_NUDGE})
    assert result["sent"] == 1


async def test_ignored_repeat_brings_another_tea(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await repeat_sent(clean, order, NOW)
    assert (await retention.check(NOW + timedelta(days=13)))["sent"] == 0  # ещё рано
    assert (await retention.check(NOW + timedelta(days=14)))["sent"] == 1
    assert outbox.client[-1] == (
        "Здравствуйте! К Те Гуань Инь 100 г у нас советуют Да Хун Пао (1500 ₽) — Утёсный улун с "
        "жареными нотами.\n"
        "Если захотите — нажмите «Взять» или просто напишите.\n"
        "Если не хотите таких сообщений — напишите «стоп»."
    )
    assert labels(outbox.boards[-1]) == ["Взять Да Хун Пао", "Повторить прошлый заказ"]
    assert (await retention.check(NOW + timedelta(days=18)))["sent"] == 0  # одно на цикл


async def test_answered_repeat_means_no_second_chance(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await repeat_sent(clean, order, NOW)
    await say(clean, "user", NOW + timedelta(hours=1))
    assert (await retention.check(NOW + timedelta(days=14)))["sent"] == 0


async def test_pressed_button_means_no_second_chance(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await repeat_sent(clean, order, NOW)
    from app.messages import funnel

    await funnel.record(PEER, "button:other", order_id=order.id, at=NOW + timedelta(hours=2))
    assert (await retention.check(NOW + timedelta(days=14)))["sent"] == 0


async def test_liked_order_says_so_and_novelty_goes_first(clean, outbox, catalog):
    catalog[2]["is_new"] = True
    order = await make_order(clean, delivered_days_ago=22)
    await feedback.rate(PEER, order.id, "great", "button")
    await repeat_sent(clean, order, NOW)
    await retention.check(NOW + timedelta(days=14))
    assert outbox.client[-1].startswith(
        "Здравствуйте! Вам понравился Те Гуань Инь 100 г — попробуйте Габа (1100 ₽) — Мягкий и сладкий."
    )


async def test_not_mine_replaces_repeat_in_its_time(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await feedback.rate(PEER, order.id, "no", "button")
    result = await retention.check(NOW)
    assert [d.kind for d in result["decisions"]] == [templates.SECOND_TOUCH]
    assert outbox.client[-1].startswith("Здравствуйте! Подобрала вам другой чай — Да Хун Пао (1500 ₽)")
    # Повторять то, что не понравилось, не предлагаем.
    assert labels(outbox.boards[-1]) == ["Взять Да Хун Пао"]


async def test_nothing_to_offer_means_no_touch(clean, outbox, catalog):
    for item in catalog[1:]:
        item["in_stock"] = False
    order = await make_order(clean, delivered_days_ago=22)
    await repeat_sent(clean, order, NOW)
    result = await retention.check(NOW + timedelta(days=14))
    assert result["sent"] == 0 and result["decisions"][0].outcome == "nothing"


async def test_never_offers_what_was_bought_or_disliked():
    bought = ["Те Гуань Инь 100 г", "Да Хун Пао"]
    picked = second_touch.pick_sorts(CATALOG, bought, set(), limit=3)
    assert [item["name"] for item, _ in picked] == ["Габа"]
    assert second_touch.pick_sorts(CATALOG, ["Те Гуань Инь 100 г"], {"габа", "да хун пао"}) == []


async def test_take_button_starts_a_draft(clean, outbox):
    order = await make_order(clean, delivered_days_ago=22)
    await repeat_sent(clean, order, NOW)
    await retention.check(NOW + timedelta(days=14))
    button = outbox.boards[-1]["buttons"][0][0]["action"]
    press = await buttons.handle(PEER, {"payload": button["payload"]})
    assert press.reply.startswith("Записала: Да Хун Пао — 1500 ₽.")
    assert [row["name"] for row in (await state.get_draft(PEER)).items] == ["Да Хун Пао"]
    assert json.loads(button["payload"])["t"] == templates.SECOND_TOUCH
    history = await dialog_history.get_history(PEER)
    assert history[-1]["content"].startswith("Здравствуйте! К Те Гуань Инь")
