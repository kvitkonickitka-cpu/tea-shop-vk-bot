"""Пакет C, первая часть: получатель одним сообщением (C1), доверие к оплате и новый
счёт при отказе банка (C2), вопрос менеджеру вне рабочего времени (C6). Всё — за флагами."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core import flags, worktime
from app.core.config import settings
from app.messages import client as client_messages, templates
from app.messages.models import FunnelEvent
from app.modules.analytics import service as analytics
from app.modules.orders import conversation, state
from app.modules.payment import methods as payment_methods, service as payment_service
from tests.test_auto_invoice import PEER, point_chosen, said, tool_use, world  # noqa: F401


@pytest.fixture
def tester(monkeypatch):
    monkeypatch.setattr(settings, "test_vk_ids", str(PEER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(settings, "enabled_payment_methods", "bank_card,sbp,sberbank,tinkoff_bank")
    payment_methods.forget_cache()


async def turn_on(*names):
    for name in names:
        await flags.set_mode(name, flags.ALLOWLIST, by="test")


async def events(db) -> list[str]:
    async with db() as session:
        return list((await session.execute(select(FunnelEvent.event).order_by(FunnelEvent.id))).scalars())


async def test_recipient_in_one_message_is_parsed_by_code(clean, world, tester):
    await turn_on(flags.RECIPIENT_ONE_MESSAGE)
    await point_chosen()
    world["script"] = []
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")
    assert world["model"] == [] and len(world["payments"]) == 1
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in reply
    assert "recipient_one_message" in await events(clean) and "invoice_auto" in await events(clean)


async def test_only_missing_parts_are_asked_again(clean, world, tester):
    await turn_on(flags.RECIPIENT_ONE_MESSAGE)
    await point_chosen()
    world["script"] = []
    first = await conversation.handle_turn(PEER, "Иванов Иван")
    assert first == "Записала ФИО. Пришлите, пожалуйста, телефон и почту для чека."
    second = await conversation.handle_turn(PEER, "89001234567")
    assert second == ("Записала телефон. Пришлите, пожалуйста, почту для чека — на неё придёт чек, "
                      "без неё оплату не выставить.")
    third = await conversation.handle_turn(PEER, "ivanov@mail.ru")
    assert "Получатель: Иванов Иван, +79001234567, ivanov@mail.ru" in third
    assert world["model"] == [] and len(world["payments"]) == 1


async def test_question_next_to_data_goes_to_the_model(clean, world, tester):
    await turn_on(flags.RECIPIENT_ONE_MESSAGE)
    await point_chosen()
    world["script"] = [said("Заваривать 90 градусов. А данные запишу.")]
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, а как заваривать?")
    assert reply.startswith("Заваривать") and len(world["model"]) == 1


async def test_flag_off_keeps_the_model_path(clean, world, tester):
    await point_chosen()
    world["script"] = [tool_use("set_recipient", name="Иванов Иван", phone="89001234567", email="ivanov@mail.ru")]
    await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")
    assert len(world["model"]) == 1 and len(world["payments"]) == 1


async def test_short_number_is_asked_again(clean, world, tester):
    await turn_on(flags.RECIPIENT_ONE_MESSAGE)
    await point_chosen()
    world["script"] = []
    # Шесть цифр — не телефон: код записывает ФИО и почту и просит номер.
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 890012, ivanov@mail.ru")
    assert reply == "Записала ФИО и почту. Пришлите, пожалуйста, телефон." and world["payments"] == []


async def test_bad_email_falls_back_to_the_model(clean, world, tester):
    await turn_on(flags.RECIPIENT_ONE_MESSAGE)
    await point_chosen()
    world["script"] = [said("Может, ivanov@yandex.ru?")]
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@yandex.ry")
    assert reply == "Может, ivanov@yandex.ru?" and world["payments"] == [] and len(world["model"]) == 1


async def test_invoice_names_only_enabled_methods_and_requisites(clean, world, tester, monkeypatch):
    monkeypatch.setattr(settings, "requisites_url", "https://vk.com/@teapotwice-rekvizity")
    await turn_on(flags.PAYMENT_TRUST)
    await point_chosen()
    world["script"] = [tool_use("set_recipient", name="Иванов Иван", phone="89001234567", email="ivanov@mail.ru")]
    reply = await conversation.handle_turn(PEER, "Иванов Иван, 89001234567, ivanov@mail.ru")
    assert "Оплата через ЮKassa — картой, СБП, SberPay или T-Pay. Чек по 54-ФЗ придёт на почту." in reply
    assert "Реквизиты ИП: https://vk.com/@teapotwice-rekvizity" in reply
    # Модель знает только эти способы.
    assert "Оплатить можно картой, СБП, SberPay или T-Pay" in world["model"][0]["prompt"]


async def test_methods_come_from_yookassa_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "enabled_payment_methods", "")
    payment_methods.forget_cache()

    async def account_info():
        return {"payment_methods": ["bank_card", "sbp"]}

    monkeypatch.setattr("app.modules.payment.yookassa_client.account_info", account_info)
    assert await payment_methods.enabled() == ["bank_card", "sbp"]
    assert templates.payment_methods_phrase(["bank_card", "sbp", "alfa_pay"]) == "картой, СБП или Alfa Pay"


async def test_declined_payment_gets_a_new_link_at_once(clean, world, tester, monkeypatch):
    from app.modules.orders import buttons
    from tests.test_paid_after_close import make_order

    await turn_on(flags.PAYMENT_RETRY)
    order = await make_order(clean, status=payment_service.STATUS_AWAITING_PAYMENT, payment_id="pay-1",
                             payment_status="pending")
    order.peer_id = PEER
    sent = []

    async def auto_invoice(peer_id):
        buttons.stash(peer_id, {"buttons": [["pay"]]})
        return conversation.ToolExecution("ok", client_reply="Заказ №1 — проверьте…\nОплатить — кнопкой ниже 👇")

    async def send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(conversation, "_auto_invoice", auto_invoice)
    monkeypatch.setattr(client_messages, "send", send)
    assert await payment_service.tell_about_closed_invoice(order, templates.PAYMENT_DECLINED)
    assert sent[0]["text"].startswith(f"Платёж по заказу №{order.id} не прошёл — банк отказал")
    assert "другой картой или СБП, SberPay или T-Pay" in sent[0]["text"] and sent[0]["keyboard"]
    assert await events(clean) == ["payment_method_switch_offered"]


async def test_after_hours_escalation_says_when_and_keeps_selling(clean, world, tester, monkeypatch):
    await turn_on(flags.ESCALATION_AFTER_HOURS)
    night = datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc)  # 00:30 МСК
    monkeypatch.setattr(worktime, "now_msk", lambda: night.astimezone(worktime.MSK))
    world["script"] = [tool_use("escalate_to_manager", question="Есть ли опт?", reason="опт"),
                       said("Передала менеджеру — ответит сегодня с 10:00. А пока: улун к сладкому — Те Гуань Инь.")]
    reply = await conversation.handle_turn(PEER, "Есть опт? И что взять к сладкому?")
    assert "сегодня с 10:00" in reply and len(world["model"]) == 2
    assert "escalation_after_hours" in await events(clean)
    assert worktime.manager_back_phrase(datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc)) == "завтра с 10:00"


async def test_daytime_escalation_unchanged(clean, world, tester, monkeypatch):
    await turn_on(flags.ESCALATION_AFTER_HOURS)
    noon = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(worktime, "now_msk", lambda: noon.astimezone(worktime.MSK))
    world["script"] = [tool_use("escalate_to_manager", question="Есть ли опт?", reason="опт")]
    reply = await conversation.handle_turn(PEER, "Есть опт?")
    assert reply == "Передала ваш вопрос менеджеру — он ответит здесь, в этом диалоге 🙏"
