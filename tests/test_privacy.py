"""Часть Б: метки вместо персональных данных — хранилище, распознавание, подстановка."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app import privacy
from app.core.client_key import client_key
from app.messages import templates
from app.modules.catalog import service as catalog_service
from app.modules.dialog import history as dialog_history, service as dialog_service
from app.modules.orders.state import OrderDraft
from app.privacy import detect, vault
from app.privacy.models import PiiEntry

PEER, OTHER = 9800, 9801
CATALOG = [
    {"name": "Да Хун Пао", "price": 1500, "in_stock": True},
    {"name": "Те Гуань Инь 100 г", "price": 1500, "in_stock": True, "synonyms": ["ТГИ", "тегуанинь", "Железная Богиня"]},
    {"name": "Габа", "price": 1100, "in_stock": True},
    {"name": "Шу Пуэр Мэнхай", "price": 900, "in_stock": True},
    {"name": "Лао Ча Тоу", "price": 1300, "in_stock": True, "synonyms": ["Чайные Головы"]},
]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(catalog_service, "load_items", lambda: CATALOG)


async def tok(text, *stage, peer=PEER, names=True):
    return await privacy.tokenize(peer, text, names=names, stage=frozenset(stage))


# --- Б1: хранилище -------------------------------------------------------------

async def test_values_are_encrypted_and_labels_are_stable(clean):
    first = await tok("Иванов Иван, 8 (900) 123-45-67, Ivanov@Mail.ru")
    assert first == "[NAME_1], [PHONE_1], [EMAIL_1]"
    # Тот же телефон в другом виде, та же почта в другом регистре — те же метки.
    vault.forget_cache()
    assert await tok("+79001234567 ivanov@mail.ru") == "[PHONE_1] [EMAIL_1]"
    assert await tok("и второй: 89007654321") == "и второй: [PHONE_2]"
    async with clean() as session:
        rows = (await session.execute(select(PiiEntry).order_by(PiiEntry.id))).scalars().all()
    assert sorted((r.label, r.kind) for r in rows) == [("EMAIL_1", "EMAIL"), ("NAME_1", "NAME"),
                                                      ("PHONE_1", "PHONE"), ("PHONE_2", "PHONE")]
    dump = " ".join(f"{r.client_key} {r.value_enc} {r.value_hash}" for r in rows)
    for value in ("Иванов", "79001234567", "ivanov", str(PEER)):
        assert value not in dump  # в базе нет ни значений, ни VK ID
    assert {r.client_key for r in rows} == {client_key(PEER)}
    # Нормализованный вид: телефон +7…, почта строчными.
    assert await privacy.detokenize(PEER, "[PHONE_1] [EMAIL_1] [NAME_1]") == "+79001234567 ivanov@mail.ru Иванов Иван"


async def test_labels_never_cross_clients(clean):
    assert await tok("89001234567") == "[PHONE_1]"
    # У другого клиента свой счёт: [PHONE_1] — другое значение.
    assert await tok("89005555555", peer=OTHER) == "[PHONE_1]"
    assert await privacy.detokenize(OTHER, "[PHONE_1]") == "+79005555555"
    with pytest.raises(privacy.UnknownLabel):
        await privacy.detokenize(OTHER, "[EMAIL_1]")


async def test_row_cannot_be_moved_to_another_client(clean):
    await tok("89001234567")
    await tok("89005555555", peer=OTHER)
    async with clean() as session:
        row = (await session.execute(select(PiiEntry).where(PiiEntry.client_key == client_key(PEER)))).scalar_one()
        row.client_key = client_key(OTHER) + "x"
        await session.commit()
    vault.forget_cache()
    book = await vault.load(client_key(OTHER) + "x")
    assert book.values == {}  # связанные данные шифра не сошлись — не расшифровали


# --- Б2: распознавание -----------------------------------------------------------

@pytest.mark.parametrize("phone", [
    "89001234567", "8 900 123 45 67", "+7 (900) 123-45-67", "+79001234567", "7 900 123-45-67",
    "8-900-123-45-67", "(900) 123-45-67", "900 123 45 67", "9001234567", "8 (495) 123-45-67",
])
async def test_every_phone_format(clean, phone):
    assert await tok(f"мой номер {phone}, звоните") == "мой номер [PHONE_1], звоните"


@pytest.mark.parametrize("text", [
    "трек 1234567890", "заказ №15 на 1621 ₽", "ИНН 231234567890", "пункт 4, Ставропольская 230",
    "отправление 0123-4567-8", "в 2026 году 12 раз",
])
async def test_numbers_that_are_not_phones(clean, text):
    assert await tok(text) == text


async def test_stop_list_keeps_teas_cities_and_carriers(clean):
    """Обязательный тест из ТЗ: сорт чая не должен стать [NAME_1]."""
    texts = [
        "Хочу Да Хун Пао и Те Гуань Инь",
        "А Габа есть? И Шу Пуэр Мэнхай",
        "Лао Ча Тоу, он же Чайные Головы",
        "Железная Богиня — это ТГИ?",
        "Доставка в Москву или Краснодар, лучше СДЭК или Ozon",
        "Во Владимир можно? А в Королёв?",
        "Почта России не нужна, Озон удобнее",
        "Краснодар, Ставропольская",
        "Москва Тверская",
        "Ставропольская улица 230, Красная 176",
        "ул. Ленина 5",
        "на проспекте Мира",
    ]
    for text in texts:
        assert await tok(text) == text, text
    for text in texts[:4]:
        assert await tok(text, privacy.RECIPIENT) == text, text


async def test_stop_list_is_built_from_the_sheet():
    stop = detect.stop_list(CATALOG)
    assert stop.product("Гуань") and stop.product("пуэр") and stop.product("головы") and stop.product("СДЭК")
    assert stop.city("Краснодар") and stop.city("владимир")


@pytest.mark.parametrize("text, expected", [
    ("Иванов Иван Иванович", "[NAME_1]"),
    ("Меня зовут Анна, хочу чай", "Меня зовут [NAME_1], хочу чай"),
    ("Квитко Никита 8 900 111 22 33", "[NAME_1] [PHONE_1]"),
    ("Получатель Петрова Мария Сергеевна", "Получатель [NAME_1]"),
    ("Владимир Петров заберёт", "[NAME_1] заберёт"),
    ("Да Хун Пао для Ольги, а Габа мне", "Да Хун Пао для [NAME_1], а Габа мне"),
])
async def test_names_in_free_text(clean, text, expected):
    assert await tok(text) == expected


async def test_recipient_stage_takes_lowercase_names(clean):
    assert await tok("1, иванов иван, 89001234567, ivanov@mail.ru", privacy.RECIPIENT) == (
        "1, [NAME_1], [PHONE_1], [EMAIL_1]"
    )
    assert await privacy.detokenize(PEER, "[NAME_1]") == "Иванов Иван"
    # Известное значение узнаётся и строчными.
    assert await tok("иванов иван") == "[NAME_1]"
    # Новое: без телефона и почты в тексте и вне этапа строчные слова — не имя.
    assert await tok("петров пётр") == "петров пётр"
    assert await tok("петров пётр", privacy.RECIPIENT) == "[NAME_2]"
    # «да, на эти данные» — не ФИО даже на этапе получателя.
    assert await tok("да, на эти данные", privacy.RECIPIENT) == "да, на эти данные"


async def test_courier_address_only_when_asked(clean):
    text = "Москва, ул. Ленина, д. 5, кв. 12"
    assert await tok(text) == text  # для поиска пункта город и улица остаются
    assert await tok(text + ", Иванов Иван", privacy.COURIER_ADDRESS) == "[ADDR_1], [NAME_1]"
    assert await privacy.detokenize(PEER, "[ADDR_1]") == text
    assert await tok("а сколько стоит?", privacy.COURIER_ADDRESS) == "а сколько стоит?"


def test_stages():
    waiting = OrderDraft(items=[{"name": "x"}], delivery_method="ozon_pvz", stage="awaiting_confirmation")
    assert privacy.RECIPIENT in privacy.stages(waiting)
    done = OrderDraft(items=[{"name": "x"}], delivery_method="ozon_pvz", details={
        "recipient_name": "a", "recipient_phone": "b", "recipient_email": "c"})
    assert privacy.stages(done) == frozenset()
    assert privacy.RECIPIENT in privacy.stages(None, "Пришлите ФИО, телефон и почту")
    courier = OrderDraft(items=[{"name": "x"}], delivery_method="cdek_courier", details={
        "recipient_name": "a", "recipient_phone": "b", "recipient_email": "c"})
    assert privacy.stages(courier, "Напишите адрес доставки") == frozenset({privacy.COURIER_ADDRESS})
    assert privacy.stages(None, "Курьер привезёт до двери — напишите адрес") == frozenset({privacy.COURIER_ADDRESS})
    # Список пунктов упоминает и курьера, и адрес — но просит адрес пункта.
    asks_point = templates.storefront_ask_point(order_id=1, items=[{"name": "x", "quantity": 1}], items_total=1,
                                                city="Краснодар", delivery_cost=121, ask="")
    assert privacy.COURIER_ADDRESS not in privacy.stages(None, asks_point)


async def test_known_values_and_storefront_address_are_replaced_whole(clean):
    address = "Россия, Краснодар, улица Ставропольская, 230, кв. 5"
    await privacy.remember_details(PEER, {"vk_order_address": address,
                                          "storefront_recipient": {"name": "Петров Пётр", "phone": "+79007654321"}})
    text = f"Заказ из витрины. Адрес: {address}. Получатель: Петров Пётр, +7 900 765-43-21."
    assert await tok(text, names=False) == "Заказ из витрины. Адрес: [ADDR_1]. Получатель: [NAME_1], [PHONE_1]."
    # Пункты выдачи — публичные, их не трогаем.
    assert await tok("1) Краснодар, Ставропольская улица, 230 — 121 ₽", names=False) == (
        "1) Краснодар, Ставропольская улица, 230 — 121 ₽"
    )


async def test_history_keeps_labels_and_manager_names(clean):
    await dialog_history.append_message(PEER, "assistant", "Иван, добрый день! Перезвоню на 89001234567.",
                                        author=dialog_history.AUTHOR_MANAGER)
    await dialog_history.append_exchange(PEER, "Анна Смирнова, anna@mail.ru", "Записала: [NAME_2], anna@mail.ru")
    history = await dialog_history.get_history(PEER)
    assert [m["content"] for m in history] == [
        "[ответ менеджера] [NAME_1], добрый день! Перезвоню на [PHONE_1].",
        "[NAME_2], [EMAIL_1]",
        "Записала: [NAME_2], [EMAIL_1]",
    ]


async def test_bot_templates_go_to_history_with_labels(clean, monkeypatch):
    from app.messages import client as client_messages

    sent = []

    async def to_vk(peer_id, text, random_id=None, keyboard=None):
        sent.append(text)

    monkeypatch.setattr(client_messages.vk_client, "send_message", to_vk)
    await privacy.remember_recipient(PEER, "Иванов Иван", "+79001234567", "ivanov@mail.ru")
    text = "Оплата получена. Получатель: Иванов Иван. Чек придёт на ivanov@mail.ru"
    await client_messages.send(peer_id=PEER, ref="order:1", event_type=templates.PAID, text=text)
    assert sent == [text]  # клиенту — значения
    assert (await dialog_history.get_history(PEER))[-1]["content"] == (
        "Оплата получена. Получатель: [NAME_1]. Чек придёт на [EMAIL_1]"
    )


# --- Б3: подстановка ----------------------------------------------------------------

async def test_tool_arguments_get_values(clean):
    await tok("Иванов Иван, 89001234567")
    data = await privacy.detokenize_data(PEER, {"name": "[NAME_1]", "phone": "[PHONE_1]", "n": 1, "x": ["[NAME_1]"]})
    assert data == {"name": "Иванов Иван", "phone": "+79001234567", "n": 1, "x": ["Иванов Иван"]}


async def test_unknown_label_is_not_sent(clean, monkeypatch, caplog):
    sent = []

    async def to_vk(peer_id, text, random_id=None, keyboard=None):
        sent.append(text)

    async def turn(peer_id, text, attached, budget_seconds=None):
        return await privacy.detokenize(peer_id, "Записала: [NAME_7]")

    monkeypatch.setattr(dialog_service.vk_client, "send_message", to_vk)
    monkeypatch.setattr(dialog_service.vk_client, "set_typing", lambda peer_id: _none())
    monkeypatch.setattr(dialog_service.orders_conversation, "handle_turn", turn)
    caplog.set_level(logging.ERROR)
    from app.modules.dialog import attachments

    await dialog_service.respond(PEER, "привет", attachments.Collected())
    assert sent == ["Извините, у меня техническая заминка — ответить сейчас не получается. "
                    "Напишите, пожалуйста, ещё раз через пару минут."]
    assert "Метка [NAME_7] не принадлежит клиенту" in caplog.text


async def _none():
    return None


# --- Б5: инструкция модели ------------------------------------------------------------

def test_prompt_explains_labels():
    from app.modules.orders import conversation

    assert "set_recipient с name=[NAME_1], phone=[PHONE_1], email=[EMAIL_1]" in conversation._PII_PROMPT
    assert "не проси клиента повторить данные" in conversation._PII_PROMPT
