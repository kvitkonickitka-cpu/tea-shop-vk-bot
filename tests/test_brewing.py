"""Повторные касания, задача 1: как заваривать — в сообщении «вручено»."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages.models import ManagerNotification
from app.modules.catalog import service as catalog_service, sheet
from app.modules.orders import delivery_events
from tests.test_feedback_repeat_optout import NOW, PEER, make_order, outbox  # noqa: F401

CSV = (
    "Название,Цена,Как заваривать,Видео\n"
    "Те Гуань Инь,1500,\"5 г на 150 мл, 85 °C, первая заварка 30 секунд\",https://vk.com/video-1_2\n"
    "Да Хун Пао,1500,95 °C и пролив 10 секунд,\n"
    "Шу Пуэр,900,,\n"
    "Габа,1100," + "долго " * 60 + ",http://example.com/v\n"
)


def test_sheet_reads_brewing_and_warns_without_blocking():
    parsed = sheet.parse_csv(CSV)
    assert parsed.errors == []  # предупреждения таблицу не блокируют
    by_name = {item["name"]: item for item in parsed.items}
    assert by_name["Те Гуань Инь"]["brewing"] == "5 г на 150 мл, 85 °C, первая заварка 30 секунд"
    assert by_name["Те Гуань Инь"]["video"] == "https://vk.com/video-1_2"
    assert by_name["Шу Пуэр"]["brewing"] == "" and by_name["Габа"]["brewing"] == ""
    assert by_name["Габа"]["video"] == "" and by_name["Габа"]["price"] == 1100
    assert parsed.warnings == [
        "строка 5 «Габа»: «Как заваривать» — 359 символов, а можно до 300; строка применена без заварки",
        "строка 5 «Габа»: «Видео» — ссылка должна начинаться с https://; применено без видео",
    ]


async def test_warning_goes_to_manager_once(clean, outbox):
    await sheet._warn(["строка 5: длинно"])
    await sheet._warn(["строка 5: длинно"])
    await sheet._warn(["строка 6: другое"])
    async with clean() as session:
        rows = (await session.execute(select(ManagerNotification))).scalars().all()
    assert len(rows) == 2 and rows[0].payload.startswith("⚠️ <b>Таблица каталога применена, но не целиком</b>")


def catalog(monkeypatch):
    items = sheet.parse_csv(CSV).items
    monkeypatch.setattr(catalog_service, "load_items", lambda: items)


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    # «Вручено» ночью не пишется — тесту не важно, когда его запустили.
    monkeypatch.setattr(delivery_events.worktime, "is_quiet", lambda now=None: False)


ITEMS = [{"name": n, "quantity": 1, "price": 1500} for n in ("Те Гуань Инь", "Шу Пуэр", "Да Хун Пао")]


async def test_delivered_with_brewing_blocks_and_guide(clean, outbox, monkeypatch):
    catalog(monkeypatch)
    monkeypatch.setattr(settings, "brewing_guide_url", "https://vk.com/@shop-brewing")
    order = await make_order(clean, items=ITEMS, delivered_at=None)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert outbox.client[-1] == (
        f"Заказ №{order.id} вручён — спасибо, что выбрали нас! 🍵\n"
        "\n"
        "Как заваривать:\n"
        "• Те Гуань Инь: 5 г на 150 мл, 85 °C, первая заварка 30 секунд\n"
        "  Видео: https://vk.com/video-1_2\n"
        "• Да Хун Пао: 95 °C и пролив 10 секунд\n"
        "Все способы заварки: https://vk.com/@shop-brewing"
    )


@pytest.mark.parametrize("ask_enabled", [True, False])
async def test_no_brewing_line_depends_on_rating_ask(clean, outbox, monkeypatch, ask_enabled):
    catalog(monkeypatch)
    monkeypatch.setattr(settings, "feedback_ask_enabled", ask_enabled)
    order = await make_order(clean, items=[{"name": "Шу Пуэр", "quantity": 1, "price": 900}], delivered_at=None)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    # Об оценке через три дня спросят кнопками — просить отзыв здесь незачем.
    tail = "" if ask_enabled else "\nБудет здорово, если напишете, как вам чай."
    assert outbox.client[-1] == f"Заказ №{order.id} вручён — спасибо, что выбрали нас! 🍵{tail}"


async def test_brewing_flag_off(clean, outbox, monkeypatch):
    catalog(monkeypatch)
    monkeypatch.setattr(settings, "brewing_in_delivered_enabled", False)
    order = await make_order(clean, items=ITEMS[:1], delivered_at=None)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert "Как заваривать" not in outbox.client[-1]


async def test_opted_out_client_still_gets_brewing(clean, outbox, monkeypatch):
    from app.messages import marketing

    catalog(monkeypatch)
    await marketing.opt_out(PEER)
    order = await make_order(clean, items=ITEMS[:1], delivered_at=None)
    await delivery_events.record(order.id, delivery_events.DELIVERED, source="тест")
    assert "Как заваривать:" in outbox.client[-1]
