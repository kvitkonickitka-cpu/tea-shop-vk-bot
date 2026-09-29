"""Задача 1 продающей части: согласованные тексты вшиты, правила main не потерялись."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import worktime
from app.core.config import settings
from app.messages import templates
from app.modules.orders import conversation, delivery_watch, repository as orders_repository
from app.modules.orders.models import Order

PROMPTS = Path(conversation.__file__).parent.parent / "dialog" / "prompts"


def test_main_rules_survived_the_merge():
    system = (PROMPTS / "system_prompt.md").read_text(encoding="utf-8")
    flow = (PROMPTS / "order_flow_prompt.md").read_text(encoding="utf-8")
    escalation = (PROMPTS / "escalation_flow_prompt.md").read_text(encoding="utf-8")

    # Пункты — только из свежего поиска, повтор доставки — только после «да».
    assert "только из последнего ответа инструмента" in flow
    assert "как в прошлый раз" in flow
    assert "Не вызывай set_delivery_method повторно без повода" in flow
    # Отмена неоплаченного — сама, без менеджера.
    assert "cancel_order" in flow and "cancel_order" in system and "cancel_order" in escalation
    # Слово менеджера главнее; без разметки.
    assert "[ответ менеджера]" in system and "главнее всего остального" in system
    assert "Пиши обычным текстом" in system
    # Новые правила из согласованных текстов.
    assert "«Два чайника»" in system and "женском роде" in system
    assert "Менеджера по таким вопросам не зови" in system
    assert "виртуальный помощник" in system
    assert "Всё верно, оформляем?" in flow
    assert "уже сообщила" in escalation


def test_health_questions_are_not_escalated_any_more():
    system = (PROMPTS / "system_prompt.md").read_text(encoding="utf-8")
    must_escalate = system.split("Когда обязательно вызывать escalate_to_manager")[1].split("Вопросы о здоровье")[0]
    assert "лечебн" not in must_escalate and "здоров" not in must_escalate


def test_payment_step_names_the_link_lifetime(monkeypatch):
    monkeypatch.setattr(conversation.payment_service, "is_enabled", lambda: True)
    assert f"Ссылка действует {settings.payment_invoice_ttl_minutes} минут" in conversation.order_flow_prompt()


@pytest.mark.parametrize("url", ["", "https://vk.com/@dva_chainika-usloviya"])
def test_invoice_summary_shows_conditions_only_when_set(monkeypatch, url):
    monkeypatch.setattr(settings, "conditions_url", url)
    text = templates.invoice_summary(
        order_id=128, items=[{"name": "Те Гуань Инь", "quantity": 2, "price": 400}],
        delivery_method="ozon_pvz", delivery_label="Ozon, пункт выдачи: Краснодар, Красная, 1",
        delivery_cost=117, name="Иванов Иван", phone="+79001234567", email="a@b.ru",
        total=917, link="https://pay/x",
    )
    assert text.startswith("Заказ №128 — проверьте, всё ли верно:\n• Те Гуань Инь × 2 — 800 ₽")
    assert "Доставка: пункт выдачи Ozon, Краснодар, Красная, 1 — 117 ₽" in text
    assert "Получатель: Иванов Иван, +79001234567, a@b.ru" in text
    assert "Итого: 917 ₽\n\nОплатить: https://pay/x" in text
    assert "Ссылка действует 60 минут" in text and "чек на a@b.ru" in text
    assert ("Условия покупки" in text) == bool(url)
    assert text.endswith("Если что-то не так — напишите, поправлю и пришлю новую ссылку.")


def test_paid_promises_the_handover_period(monkeypatch):
    monkeypatch.setattr(settings, "handover_promise", "в течение 1–2 дней")
    order = SimpleNamespace(id=128, total=917)
    ozon = templates.paid(order, email="a@b.ru", posting="0123-4567-8")
    assert "Соберём посылку и сдадим в Ozon в течение 1–2 дней. Номер отправления: 0123-4567-8" in ozon
    cdek = templates.paid(order, email="a@b.ru", cdek=True)
    assert "Соберём посылку и сдадим в СДЭК в течение 1–2 дней" in cdek


def test_carrier_failed_card_has_the_order_number():
    text = templates.manager_carrier_failed(128, "Ozon", "https://vk.com/gim1?sel=2")
    assert text.startswith("⚠️ Заказ №128 оплачен, но в Ozon не уехал.")


def test_no_rubles_left_in_templates():
    source = Path(templates.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]  # без заголовка модуля
    assert " руб" not in body


def test_receipt_threshold_is_the_next_business_morning():
    friday_evening = datetime(2026, 9, 25, 20, 0, tzinfo=worktime.MSK)
    monday = worktime.next_business_morning(friday_evening)
    assert (monday.weekday(), monday.hour) == (0, settings.manager_work_hours_start)
    tuesday = worktime.next_business_morning(datetime(2026, 9, 28, 9, 0, tzinfo=worktime.MSK))
    assert tuesday.date().isoformat() == "2026-09-29"


async def test_not_handed_over_is_reported_once(clean, monkeypatch):
    sent = []

    async def to_manager(order, text):
        sent.append(text)

    monkeypatch.setattr(delivery_watch.order_chat, "send", to_manager)
    monkeypatch.setattr(settings, "handover_days", 2)
    now = datetime.now(timezone.utc)
    async with clean() as session:
        late = Order(
            peer_id=7100, items=[], items_total=100, total=217, status="paid",
            payment_status=orders_repository.PAID, ozon_posting="0001-1",
            paid_at=now - timedelta(days=3), created_at=now - timedelta(days=3),
        )
        fresh = Order(
            peer_id=7101, items=[], items_total=100, total=217, status="paid",
            payment_status=orders_repository.PAID, ozon_posting="0001-2",
            paid_at=now - timedelta(hours=5), created_at=now - timedelta(hours=5),
        )
        session.add_all([late, fresh])
        await session.commit()

    assert await delivery_watch.report_not_handed_over(now) == 1
    assert await delivery_watch.report_not_handed_over(now) == 0
    assert len(sent) == 1 and sent[0].startswith(f"⏰ Заказ №{late.id} оплачен")
    assert "ещё не сдан в Ozon" in sent[0] and "в течение 1–2 дней" in sent[0]
