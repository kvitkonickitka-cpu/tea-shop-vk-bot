"""Согласие на обработку ПДн и на рассылку: ворота, журнал, отзыв, маскировка.

Здесь журнал настоящий (маркер `real_consent`): в остальных тестах согласие
считается данным, чтобы сценарии заказа проверяли заказ.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.messages import manager as manager_messages, templates
from app.messages.models import ManagerNotification
from app.modules.catalog import service as catalog_service
from app.modules.consent import erase, flow, service as consent
from app.modules.consent.models import Consent
from app.modules.dialog import history as dialog_history, inbound
from app.modules.orders import buttons, conversation, service as orders_service, state, vk_orders_client
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft
from app.privacy.models import PiiEntry

pytestmark = pytest.mark.real_consent

PEER = 9770
CATALOG = [{"name": "Те Гуань Инь (тест)", "price": 1500, "in_stock": True, "recommended": []}]
PHONE, EMAIL, STREET = "89001234567", "ivanov@mail.ru", "ул. Ленина, д. 5, кв. 12"


def tool_use(tool: str, **tool_input):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t-{tool}", name=tool, input=tool_input)])


def said(text: str):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])


@pytest.fixture
def world(monkeypatch):
    box = {"model": [], "script": [], "vk": [], "manager": []}

    async def converse(messages, system_prompt, tools, **_):
        box["model"].append({"messages": messages, "prompt": system_prompt, "tools": [t["name"] for t in tools]})
        return box["script"].pop(0)

    async def to_client(peer_id, text, random_id=None, **_):
        box["vk"].append(text)

    async def to_manager(text, chat_id=None):
        box["manager"].append(text)

    monkeypatch.setattr(conversation.claude_client, "converse", converse)
    monkeypatch.setattr(catalog_service, "load_items", lambda: [dict(i) for i in CATALOG])
    monkeypatch.setattr("app.messages.client.vk_client.send_message", to_client)
    monkeypatch.setattr(manager_messages.telegram_client, "send_message", to_manager)
    return box


async def cart() -> OrderDraft:
    draft = OrderDraft(items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}], items_total=1500,
                       stage="awaiting_delivery")
    await state.set_draft(PEER, draft)
    return draft


def press(action: str, **payload) -> dict:
    return {"peer_id": PEER, "text": action, "payload": json.dumps({"a": action, **payload})}


async def journal_rows(db, kind=None) -> list[Consent]:
    async with db() as session:
        query = select(Consent).where(Consent.vk_user_id == PEER).order_by(Consent.id)
        if kind:
            query = query.where(Consent.type == kind)
        return list((await session.execute(query)).scalars())


# --- ворота -----------------------------------------------------------------


async def test_without_consent_no_data_is_asked_or_written(clean, world):
    await cart()
    world["script"] = [
        tool_use("set_recipient", name="Иванов Иван", phone="+79001234567", email=EMAIL),
        said("Состав записала: Те Гуань Инь — 1500 ₽."),
    ]
    reply = await conversation.handle_turn(PEER, "беру, оформляйте")

    first, second = world["model"]
    assert templates.pd_consent_needed_note()[:60] in first["prompt"]
    # Ворота — в коде: инструмент не выполнен, получатель не записан.
    result = second["messages"][-1]["content"][0]["content"]
    assert result.startswith("НЕ ВЫПОЛНЕНО")
    draft = await state.get_draft(PEER)
    assert not draft.details.get("recipient_phone") and not draft.details.get("recipient_email")
    # К ответу — просьба о согласии со ссылкой.
    assert settings.pd_consent_url in reply and "Согласен" in reply
    async with clean() as session:
        assert (await session.execute(select(Order))).first() is None


async def test_cart_without_consent_drops_delivery_hint(clean, world):
    world["script"] = [
        tool_use("propose_order", items=[{"name": "Те Гуань Инь", "quantity": 1}],
                 delivery_hint="Москва, ул. Ленина 5"),
        said("Записала Те Гуань Инь — 1500 ₽."),
    ]
    reply = await conversation.handle_turn(PEER, "возьму те гуань инь")
    result = world["model"][1]["messages"][-1]["content"][0]["content"]
    assert "Согласия клиента на обработку" in result and "Ленина" not in result
    draft = await state.get_draft(PEER)
    assert draft is not None and draft.items[0]["name"] == "Те Гуань Инь (тест)" and not draft.delivery_method
    assert settings.pd_consent_url in reply
    # Запрос отмечен — «согласен» словами теперь засчитается.
    assert await consent.pd_prompt_pending(PEER)


async def test_set_draft_backstop_strips_personal_data(clean, world):
    draft = await cart()
    draft.details.update({"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
                          "vk_order_address": "Москва, Ленина 5"})
    await state.set_draft(PEER, draft)
    stored = await state.get_draft(PEER)
    assert not {"recipient_name", "recipient_phone", "vk_order_address"} & set(stored.details)


async def test_button_with_data_asks_for_consent(clean, world):
    draft = await cart()
    result = await buttons.handle(PEER, press("last_recipient", v=draft.details["version"]))
    assert result.reply.startswith(templates.pd_consent_request(settings.pd_consent_url)[:40])
    assert not result.to_model


# --- «Согласен» / «Не согласен» ------------------------------------------------


async def test_agree_continues_from_the_same_step(clean, world):
    await cart()
    await consent.claim_pd_prompt(PEER)
    result = await buttons.handle(PEER, press("pd_yes"))
    assert templates.pd_consent_thanks() in result.reply and templates.ASK_WHERE in result.reply
    assert await consent.has_pd(PEER)
    [row] = await journal_rows(clean)
    assert (row.type, row.accepted, row.version, row.consent_url, row.method) == (
        consent.PD, True, settings.pd_consent_version, settings.pd_consent_url, "button")
    assert row.created_at.tzinfo is not None
    # Корзина на месте — следующий шаг тот же, что был.
    assert (await state.get_draft(PEER)).items[0]["name"] == "Те Гуань Инь (тест)"


async def test_agree_by_words_only_while_asked(clean, world):
    assert await flow.intercept(PEER, "согласен") is None  # не спрашивали — обычная реплика
    await cart()
    await consent.claim_pd_prompt(PEER)
    consent.set_evidence({"id": 555}, "event-1")
    answered = await flow.intercept(PEER, "Согласна!")
    assert answered is not None and await consent.has_pd(PEER)
    [row] = await journal_rows(clean)
    assert (row.method, row.vk_message_id, row.event_id) == ("text", 555, "event-1")


async def test_decline_keeps_the_consultation_going(clean, world):
    await cart()
    await consent.claim_pd_prompt(PEER)
    result = await buttons.handle(PEER, press("pd_no"))
    assert result.reply == templates.pd_consent_declined()
    assert not await consent.has_pd(PEER) and await consent.pd_declined(PEER)

    world["script"] = [said("Те Гуань Инь заваривают 90 градусами.")]
    reply = await conversation.handle_turn(PEER, "как его заваривать?")
    assert templates.pd_consent_declined_note()[:50] in world["model"][0]["prompt"]
    # Консультация без повторной просьбы и без вопроса о данных.
    assert reply == "Те Гуань Инь заваривают 90 градусами."

    # «Оформить заказ» после отказа — запрос снова.
    retry = await buttons.handle(PEER, press("pd_retry"))
    assert settings.pd_consent_url in retry.reply


async def test_repeated_press_appends_to_the_journal(clean, world):
    await cart()
    await buttons.handle(PEER, press("pd_yes"))
    await buttons.handle(PEER, press("pd_yes"))
    rows = await journal_rows(clean, consent.PD)
    assert len(rows) == 2 and all(r.accepted for r in rows) and rows[0].id != rows[1].id


async def test_new_version_asks_again(clean, world, monkeypatch):
    await consent.record(PEER, consent.PD, True, method="button")
    assert await consent.has_pd(PEER)
    monkeypatch.setattr(settings, "pd_consent_version", "2")
    assert not await consent.has_pd(PEER)
    draft = await cart()
    result = await buttons.handle(PEER, press("pt", n=1, v=draft.details["version"]))
    assert settings.pd_consent_url in result.reply


# --- данные до согласия ------------------------------------------------------------


async def test_phone_before_consent_is_not_saved(clean, world):
    await cart()
    message = await inbound._without_pd_before_consent(
        PEER, {"peer_id": PEER, "text": f"Иванов Иван, {PHONE}, {EMAIL}, {STREET}"})
    assert PHONE not in message["text"] and EMAIL not in message["text"] and "Ленина" not in message["text"]
    assert consent.HIDDEN in message["text"]
    async with clean() as session:
        assert (await session.execute(select(PiiEntry))).first() is None
    # После согласия — просьба прислать снова.
    result = await buttons.handle(PEER, press("pd_yes"))
    assert templates.pd_resend() in result.reply

    # С согласием сообщение не трогаем.
    kept = await inbound._without_pd_before_consent(PEER, {"peer_id": PEER, "text": PHONE})
    assert kept["text"] == PHONE


async def test_geo_before_consent_is_dropped(clean, world):
    message = await inbound._without_pd_before_consent(PEER, {
        "peer_id": PEER, "text": "", "geo": {"coordinates": {"latitude": 45.0351, "longitude": 38.9753}}})
    assert "geo" not in message and consent.HIDDEN in message["text"]


# --- витрина ---------------------------------------------------------------------


async def test_storefront_order_waits_for_consent(clean, world, monkeypatch):
    async def get_order(order_id, user_id=None):
        return {"id": order_id, "user_id": PEER, "delivery_address": "Краснодар, ул. Ставропольская, 230",
                "recipient": {"name": "Иванов Иван", "phone": PHONE}}

    async def no_quotes(draft, city, street):
        return []

    monkeypatch.setattr(vk_orders_client, "get_order", get_order)
    monkeypatch.setattr(orders_service, "_quote_both", no_quotes)

    await orders_service.start_order(PEER, 77, items=[{"name": "Те Гуань Инь (тест)", "price": 1500, "quantity": 1}],
                                     address="Краснодар, ул. Ставропольская, 230", recipient=("Иванов Иван", PHONE),
                                     source="market_order_new")
    draft = await state.get_draft(PEER)
    assert draft.details["vk_order_id"] == 77 and draft.details.get("consent_wait")
    assert not draft.details.get("vk_order_address") and not draft.details.get("storefront_recipient")
    assert "Заказ №77 принят" in world["vk"][-1] and settings.pd_consent_url in world["vk"][-1]

    await buttons.handle(PEER, press("pd_yes"))
    draft = await state.get_draft(PEER)
    assert "Ставропольская" in draft.details["vk_order_address"] and not draft.details.get("consent_wait")


# --- рассылка ------------------------------------------------------------------------


async def test_marketing_filter(clean):
    a, b, c, d = 101, 102, 103, 104
    await consent.record(a, consent.MARKETING, True, method="button")
    await consent.record(b, consent.MARKETING, True, method="button")
    await consent.revoke(b, (consent.MARKETING,), method="unsubscribe")
    await consent.record(c, consent.MARKETING, False, method="button")
    assert await consent.marketing_filter([a, b, c, d]) == {a}
    assert await consent.marketing_allowed(a) and not await consent.marketing_allowed(d)
    # «С чем советуем», «Повторить», реактивация — реклама; просьба оценить — нет.
    from app.modules.orders import retention

    assert templates.FEEDBACK_ASK not in retention.ADVERTISING and templates.REPEAT_NUDGE in retention.ADVERTISING


async def test_marketing_question_once_and_unsubscribe(clean, world):
    assert await consent.ask_marketing_once(PEER)
    assert not await consent.ask_marketing_once(PEER)  # второй раз — нет
    assert await consent.marketing_question_pending(PEER)
    answered = await flow.intercept(PEER, "да")
    assert answered.reply.startswith(templates.marketing_consent_yes()[:30])
    assert await consent.marketing_allowed(PEER)
    assert not await consent.ask_marketing_once(PEER)

    await flow.intercept(PEER, "Отписаться")
    assert not await consent.marketing_allowed(PEER)
    last = (await journal_rows(clean, consent.MARKETING))[-1]
    assert last.revoked_at is not None and last.revoke_source == consent.REVOKE_BOT


async def test_stop_also_revokes_marketing(clean):
    from app.messages import marketing

    await consent.record(PEER, consent.MARKETING, True, method="button")
    await marketing.opt_out(PEER)
    assert not await consent.marketing_allowed(PEER)


# --- отзыв и обезличивание --------------------------------------------------------------


async def paid_order(db) -> Order:
    details = {"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567", "recipient_email": EMAIL,
               "address": "Краснодар", "ozon_point_id": 7}
    async with db() as session:
        order = Order(peer_id=PEER, items=[{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}],
                      items_total=1500, total=1621, delivery_method="ozon_pvz", status="delivered",
                      payment_status="succeeded", details=details)
        session.add(order)
        await session.flush()
        order.delivered_at = order.created_at
        session.add(OrderPayment(payment_id="pay-1", order_id=order.id, attempt=1, status="succeeded",
                                 amount=1621, snapshot={"items": order.items, "details": dict(details)}))
        await session.commit()
        return order


async def test_revoke_keeps_orders_and_creates_a_task(clean, world):
    order = await paid_order(clean)
    await consent.record(PEER, consent.PD, True, method="button")
    await consent.record(PEER, consent.MARKETING, True, method="button")
    await cart()

    answered = await flow.intercept(PEER, "Отзываю согласие на обработку данных")
    assert answered.reply == templates.pd_revoked()
    assert not await consent.has_pd(PEER) and not await consent.marketing_allowed(PEER)
    revoked = [r for r in await journal_rows(clean) if r.revoked_at is not None]
    assert {r.type for r in revoked} == {consent.PD, consent.MARKETING}
    assert all(r.revoke_source == consent.REVOKE_BOT for r in revoked)
    assert await state.get_draft(PEER) is None
    async with clean() as session:
        task = (await session.execute(select(ManagerNotification).where(
            ManagerNotification.kind == manager_messages.PD_DELETE))).scalar_one()
        kept = await session.get(Order, order.id)
    assert f"Удалить ПДн покупателя {PEER}" in task.payload
    # Заказ и чек не тронуты: удаление — после проверки, командой менеджера.
    assert kept.details["recipient_phone"] == "+79001234567" and kept.payment_status == "succeeded"
    # Журнал только дописывается: прежние согласия на месте.
    assert len(await journal_rows(clean)) == 4


async def test_anonymize_keeps_sums_items_and_receipts(clean, world):
    order = await paid_order(clean)
    await dialog_history.append_message(PEER, "user", f"мой телефон {PHONE}")
    dry = await erase.anonymize(PEER)
    assert dry["заказы: обезличить"] == [order.id] and dry["переписка"] == 1
    async with clean() as session:
        assert (await session.get(Order, order.id)).details["recipient_email"] == EMAIL  # пробный прогон

    await erase.anonymize(PEER, apply=True)
    async with clean() as session:
        kept = await session.get(Order, order.id)
        payment = await session.get(OrderPayment, "pay-1")
        vault = (await session.execute(select(PiiEntry))).first()
    assert kept.details["recipient_name"] == erase.ERASED and kept.details["recipient_email"] == erase.ERASED
    assert kept.details["ozon_point_id"] == 7 and float(kept.total) == 1621
    assert kept.items == [{"name": "Те Гуань Инь (тест)", "quantity": 1, "price": 1500}]
    assert payment.snapshot["details"]["recipient_phone"] == erase.ERASED
    assert payment.snapshot["items"][0]["name"] == "Те Гуань Инь (тест)"
    assert vault is None and await dialog_history.get_history(PEER) == []


async def test_paid_order_in_transit_is_not_anonymized(clean):
    order = await paid_order(clean)
    async with clean() as session:
        row = await session.get(Order, order.id)
        row.delivered_at = None
        await session.commit()
    result = await erase.anonymize(PEER, apply=True)
    assert result["заказы в пути — не тронуты (force=1 — всё равно)"] == [order.id]
    async with clean() as session:
        assert (await session.get(Order, order.id)).details["recipient_email"] == EMAIL


async def test_journal_export(clean):
    consent.set_evidence({"id": 9}, "ev-9")
    await consent.record(PEER, consent.PD, True, method="button")
    [entry] = await consent.journal(PEER)
    assert entry["type"] == consent.PD and entry["accepted"] and entry["vk_message_id"] == 9
    assert entry["created_at"].endswith("+00:00")


# --- маскировка перед моделью ------------------------------------------------------------


async def test_free_text_contacts_do_not_reach_the_model(clean, world):
    await consent.record(PEER, consent.PD, True, method="button")
    world["script"] = [said("Поняла, записала адрес.")]
    await conversation.handle_turn(PEER, f"привезите на {STREET}, звоните {PHONE}, почта {EMAIL}, Иванов Иван")
    sent = json.dumps([world["model"][0]["messages"], world["model"][0]["prompt"]], ensure_ascii=False)
    assert PHONE not in sent and "9001234567" not in sent and EMAIL not in sent
    words = json.dumps(world["model"][0]["messages"], ensure_ascii=False)  # в промпте «Ленина» — пример
    assert "Ленина" not in words and "кв. 12" not in words and "Иванов" not in words
    assert "[PHONE_1]" in sent and "[EMAIL_1]" in sent and "[ADDR_1]" in sent


async def test_escalation_to_telegram_goes_with_labels(clean, world):
    await consent.record(PEER, consent.PD, True, method="button")
    world["script"] = [tool_use("escalate_to_manager", question="Перезвонить клиенту на [PHONE_1]",
                                reason="просит звонок")]
    await conversation.handle_turn(PEER, f"перезвоните мне на {PHONE}")
    async with clean() as session:
        payload = (await session.execute(select(ManagerNotification.payload))).scalar_one()
    assert PHONE not in payload and "+79001234567" not in payload and "[PHONE_1]" in payload


def test_street_address_needs_a_house_number():
    """Улица с домом — адрес; граммы, пачки и улица без дома — нет."""
    from app.privacy import detect

    assert detect.street_addresses("Сенча 100 г, Иванова 3 пачки, на Ставропольской") == []
    assert [f.value for f in detect.street_addresses("на ул. Ленина 5а")] == ["ул. Ленина 5а"]
    assert [f.value for f in detect.street_addresses("Невский проспект 25")] == ["Невский проспект 25"]


async def test_consent_link_goes_without_preview_card(monkeypatch):
    """Карточка статьи под ссылкой загораживала просьбу о согласии — только ссылка."""
    import httpx

    from app.modules.dialog import vk_client

    sent = []

    def handler(request):
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"response": 1})

    original = httpx.AsyncClient

    class Mocked(original):
        def __init__(self, *args, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(vk_client.httpx, "AsyncClient", Mocked)
    await vk_client.send_message(1, templates.pd_consent_request(settings.pd_consent_url))
    await vk_client.send_message(1, "Те Гуань Инь заваривают 90 градусами.")
    assert sent[0].get("dont_parse_links") == "1" and "dont_parse_links" not in sent[1]
