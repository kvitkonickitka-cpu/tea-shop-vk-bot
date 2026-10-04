from __future__ import annotations

import asyncio
import contextvars
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app import privacy
from app.core.config import free_delivery_threshold, settings
from app.modules.catalog import service as catalog_service
from app.modules.delivery import cdek_client, ozon_catalog, ozon_client, ozon_quote
from app.messages import client as client_messages, funnel, manager as manager_messages, marketing, templates
from app.modules.dialog import (
    attachments as vk_attachments,
    claude_client,
    escalation_log,
    escalation_state,
    vk_client,
    history as dialog_history,
)
from app.modules.dialog.claude_client import _BASE_SYSTEM_PROMPT
from app.modules.orders import address as address_parser
from app.modules.orders import cancellation
from app.modules.orders import contacts
from app.modules.orders import eta
from app.modules.orders import feedback
from app.modules.orders import order_chat
from app.modules.orders import points
from app.modules.orders import purchases
from app.modules.orders import repeat_delivery
from app.modules.orders import repeat_order as repeat_one_tap
from app.modules.orders import service as orders_service
from app.modules.orders import shipping
from app.modules.orders import repository as orders_repository
from app.modules.orders import state
from app.modules.orders import upgrade
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service
from app.modules.payment import yookassa_client

logger = logging.getLogger(__name__)

_ESCALATION_FLOW_PROMPT_PATH = Path(__file__).parent.parent / "dialog" / "prompts" / "escalation_flow_prompt.md"
_ESCALATION_FLOW_PROMPT = _ESCALATION_FLOW_PROMPT_PATH.read_text(encoding="utf-8")

_TARIFFS_PATH = Path(__file__).parent / "delivery_tariffs.json"

# Карта пунктов выдачи: адрес пункта спрашиваем у клиента словами, а ссылку
# даём, чтобы он мог свериться. Тянуть список пунктов из API в путь обработки
# сообщения нельзя — не укладываемся в 8 секунд, которые даёт VK.
CDEK_OFFICES_MAP_URL = "https://www.cdek.ru/ru/offices"

# То же для Ozon. В городе-миллионнике пунктов десятки, и показать клиенту
# пять первых из каталога — это выбрать за него. Пусть смотрит на карте и
# называет удобный, а мы найдём его в своей копии каталога.
OZON_POINTS_MAP_URL = "https://www.ozon.ru/geo/"

# Последнее средство: модель не написала ни слова даже тогда, когда её
# позвали без инструментов. Лучше нейтральная фраза, чем извинение за
# несуществующую поломку.
# Уходит, когда ход упёрся в лимит, а модель не написала ни слова — то есть
# действие клиента могло и не выполниться. Прежнее «Записала, спасибо!»
# выдавало такой сбой за успех.
_NO_TEXT_FALLBACK = (
    "Не уверена, что правильно вас поняла. Напишите, пожалуйста, ещё раз, что "
    "нужно сделать, — проверю 🙏"
)

# Сколько раз за ход модель может попросить инструменты. Круг был всего
# один: второе обращение к Claude не помещалось в восемь секунд VK, и после
# него оставалось только отдать заглушку. Очередь этот потолок сняла —
# у контейнера 60 секунд, — а ограничение осталось, и клиент, спросивший
# цену доставки, читал в ответ «Записала, спасибо».
_MAX_TOOL_ROUNDS = 4
# Запас до потолка контейнера. Упереться в него значит не ответить вовсе,
# поэтому лучше ответить словами, не доделав последнее действие.
_TURN_BUDGET_SECONDS = 35
# Сколько ждём цены по всем показанным пунктам Ozon разом. Дольше — цена
# «около» по первому, как было: ход не должен вставать из-за перевозчика.
_PRICE_ALL_SECONDS = 4

# Первый ход после старта контейнера идёт дольше: прогреваются соединения,
# пусты все кэши. Отмечаем его в логе, чтобы не искать причину там, где её нет.
_cold_start = True

_ORDER_FLOW_PROMPT_PATH = Path(__file__).parent.parent / "dialog" / "prompts" / "order_flow_prompt.md"

# Шаг оплаты зависит от того, подключена ли касса, и описывать его в промпте
# одним текстом нельзя. Пока оплаты не было, инструкция говорила «ссылку
# пришлёт менеджер»; кассу включили, а инструкция осталась — и модель могла
# пообещать клиенту менеджера ровно перед тем, как бот сам выдаст ссылку.
def _payment_step_with_kassa() -> str:
    # Срок ссылки — из настройки: перейдём на счета ЮKassa с другим сроком —
    # текст поменяется сам.
    return (
        "Счёт со сводкой заказа и ссылкой на оплату присылает клиенту код — "
        "свою ссылку не придумывай и оплату до этого не обещай. Ссылка "
        f"действует {settings.payment_invoice_ttl_minutes} минут. "
        "Чек об оплате придёт на почту клиента. Посылка уезжает к перевозчику "
        "только после оплаты, поэтому не говори, что заказ уже отправлен или "
        "передан в доставку."
    )
_PAYMENT_STEP_WITHOUT_KASSA = (
    "Ссылку на оплату бот не выставляет: её пришлёт менеджер, инструмент сам "
    "передаст ему заказ. Не обещай клиенту оплату «сейчас» и не придумывай ссылок."
)

_ORDER_FLOW_TEMPLATE = _ORDER_FLOW_PROMPT_PATH.read_text(encoding="utf-8")


def order_flow_prompt() -> str:
    """Инструкция по оформлению заказа под текущие настройки.

    Собирается на каждый ход, а не один раз при импорте: шаг оплаты зависит
    от того, подключена ли касса, и зафиксированный при загрузке модуля
    текст переживал бы смену настройки — ровно так инструкция и разошлась с
    кодом, обещая клиенту ссылку от менеджера при работающей кассе.

    Ссылки на карты подставляем из кода, а не пишем в промпт руками: иначе
    они разъедутся с теми, что возвращают инструменты, и бот начнёт слать
    две разные.
    """
    return (
        _ORDER_FLOW_TEMPLATE
        .replace("{map_url}", CDEK_OFFICES_MAP_URL)
        .replace(
            "{payment_step}",
            _payment_step_with_kassa()
            if payment_service.is_enabled()
            else _PAYMENT_STEP_WITHOUT_KASSA,
        )
    )

# Способы доставки, которые бот вправе предложить. Почта России выключена
# настройкой: считать её по-настоящему мы не умеем, а плоский тариф — это
# цифра из воздуха. Список собирается здесь, чтобы включение было настройкой,
# а не правкой схемы инструмента.
DELIVERY_METHODS = ["cdek_pvz", "cdek_courier", "ozon_pvz"] + (
    ["russian_post"] if settings.russian_post_enabled else []
)
_OTHER_METHODS_HINT = "Почта России — тоже. " if settings.russian_post_enabled else ""

# Почту спрашиваем только когда подключена оплата: «Чеки от ЮKassa»
# доставляют чек исключительно письмом, и `customer.email` обязателен в
# каждом чеке — и в чеке оплаты, и в закрывающем при вручении (подтверждено
# поддержкой ЮKassa 25.09.2026). Одно время почта была необязательной, а чек
# уходил на телефон: тестовый магазин с эмуляцией кассы такое принимал, а
# боевой на «Чеках от ЮKassa» — нет. Пока оплаты нет, лишний вопрос клиенту
# ни к чему.
#
# Условие везде одно — `payment_service.is_enabled()`, то есть флаг И ключи.
# Раньше часть проверок смотрела на один флаг: с поднятым флагом, но без
# ключей в ревизии бот спрашивал бы у клиента почту, а оплату всё равно
# уводил менеджеру. Так ошибка настройки становилась видна клиенту.
_EMAIL_TOOL_HINT = (
    "Почта обязательна: без неё платёжная система не выставит счёт, потому что "
    "чек об оплате отправляется только на почту. Спроси её вместе с ФИО и "
    "телефоном и объясни, зачем она нужна. Если клиент отказывается — объясни "
    "один раз; при повторном отказе вызови escalate_to_manager с причиной "
    "«клиент не хочет давать почту для чека»."
    if payment_service.is_enabled()
    else ""
)

TOOLS = [
    {
        "name": "propose_order",
        "description": (
            "Зафиксировать список товаров, которые клиент хочет заказать, "
            "когда он явно выразил намерение купить и назвал товары."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Название товара, максимально близкое к названию в ассортименте",
                            },
                            "quantity": {"type": "integer"},
                        },
                        "required": ["name", "quantity"],
                    },
                },
                "delivery_hint": {
                    "type": "string",
                    "description": (
                        "Только если клиент в этом же сообщении назвал, куда везти: "
                        "город, улицу, адрес или пункт («в Москву», «на Ленина»). "
                        "Его слова как есть. Не назвал — не передавай."
                    ),
                },
                "recipient_hint": {
                    "type": "string",
                    "description": (
                        "Только если клиент в этом же сообщении назвал получателя или "
                        "его данные («на маму», «получит сестра»). Его слова как есть. "
                        "Не назвал — не передавай."
                    ),
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "set_delivery_method",
        "description": (
            "Зафиксировать выбранный клиентом способ доставки, когда есть "
            "активный черновик заказа, ожидающий выбора доставки. Для "
            "cdek_pvz, cdek_courier и ozon_pvz стоимость считается у "
            "перевозчика по-настоящему, поэтому нужен город клиента — если "
            "клиент его ещё не назвал, сначала спроси, а инструмент вызывай "
            "уже с ним. Для Ozon инструмент вместе с ценой вернёт список "
            "пунктов выдачи города — перечисли их клиенту и спроси, какой "
            "ему удобнее. "
            "Когда клиент хочет другой пункт выдачи или спрашивает, какие "
            "пункты есть на улице или в районе, — вызови инструмент снова, "
            "с этим адресом в pickup_point: список пунктов бывает только из "
            "инструмента, по памяти и из прошлых сообщений его не называй. "
            "Без такого повода повторно не вызывай: чтобы прислать карту "
            "пунктов или ответить на другой вопрос, инструмент не нужен."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": DELIVERY_METHODS,
                },
                "address": {
                    "type": "string",
                    "description": (
                        "Город клиента, а для доставки курьером — полный адрес. "
                        "Обязателен для cdek_pvz, cdek_courier и ozon_pvz."
                    ),
                },
                "pickup_point": {
                    "type": "string",
                    "description": (
                        "Для cdek_pvz и ozon_pvz: номер пункта из показанного "
                        "списка («1», «второй»), или улица, район, адрес пункта — "
                        "ровно то, что клиент сказал про этот заказ: номер дома "
                        "из прошлых заказов не подставляй. Поле необязательное: "
                        "без него инструмент вернёт пункты города с номерами. "
                        "Номер сводится к пункту из показанного списка, адрес — "
                        "к пункту из списка или из нового поиска."
                    ),
                },
            },
            "required": ["method"],
        },
    },
    {
        "name": "set_recipient",
        "description": (
            "Записать получателя заказа и почту для чека. ФИО и телефон нужны, "
            "чтобы завести отправление у СДЭКа и Ozon; почта нужна для чека об "
            "оплате — без неё счёт не выставится. Спрашивай всё одним "
            "сообщением после того, как клиент выбрал доставку и пункт выдачи. "
            "Если инструмент вернул, что телефон или почта неверны или похожи на "
            "опечатку, — передай это клиенту и попроси исправить. "
            + _EMAIL_TOOL_HINT
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "ФИО получателя"},
                "phone": {
                    "type": "string",
                    "description": "Телефон получателя, целиком, 11 цифр",
                },
                "email": {
                    "type": "string",
                    "description": (
                        "Электронная почта клиента для чека — обязательная. "
                        "Записывай ровно так, как написал клиент, не исправляй сама."
                    ),
                },
            },
            "required": ["name", "phone"],
        },
    },
    {
        "name": "confirm_order",
        "description": (
            "Выставить счёт по черновику. Обычно счёт выставляет сам код, как "
            "только выбран пункт и записан получатель, — тогда этот инструмент "
            "не нужен. Вызывай его, только когда инструмент попросил спросить "
            "клиента «Оформляем?» (например, изменился итог), и клиент ответил "
            "согласием."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_to_order",
        "description": (
            "Добавить товары в уже собранный черновик — когда клиент согласился "
            "на предложенное дополнение или сам решил докупить. Названия — как в "
            "ассортименте. Если доставка уже была посчитана, она сбросится: вес "
            "и цена посылки изменились, инструмент скажет, как пересчитать."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["name", "quantity"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "accept_offer",
        "description": (
            "Клиент согласился на заказ «как в прошлый раз», который бот показал "
            "одним сообщением (состав, пункт, получатель, итог), — ответил «да», "
            "«оформляйте» и т. п. Инструмент запишет доставку и получателя и "
            "пришлёт счёт. Если клиент хочет что-то поменять — не вызывай, "
            "используй обычные инструменты."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_order",
        "description": (
            "Отменить заказ целиком, когда клиент явно просит отменить его или "
            "передумал покупать («отмените заказ», «не надо, передумал»). "
            "Инструмент сам отменит черновик и заказ, который ещё не оплачен, "
            "— менеджер для этого не нужен. Оплаченный заказ он не отменяет и "
            "скажет, что делать дальше. Не вызывай, если клиент меняет способ "
            "доставки, пункт выдачи или состав («отмена, давайте через "
            "Ozon») — это правка заказа, для неё свои инструменты."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "escalate_to_manager",
        "description": (
            "Передать вопрос клиента живому менеджеру. Есть два независимых "
            "повода вызвать этот инструмент: (1) не хватает информации для "
            "точного ответа (например, нет данных в ассортименте или клиент "
            "спрашивает то, что не входит в компетенцию бота); (2) клиент "
            "явно просит позвать/подключить менеджера, администратора или "
            "живого человека — в любой формулировке, серьёзной или в шутку "
            "(«позови менеджера», «дай поговорить с человеком», «позови "
            "кожаного» и т.п.) — в этом случае вызывай инструмент даже если "
            "сам вопрос клиента выглядит абсурдным или не по теме. Никогда "
            "не говори клиенту «напишите менеджеру» — вместо этого вызови "
            "этот инструмент."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Коротко: о чём именно спрашивает клиент",
                },
                "reason": {
                    "type": "string",
                    "description": "Почему бот не может ответить сам (например, каких данных не хватает)",
                },
                "complaint": {
                    "type": "boolean",
                    "description": (
                        "true — клиент жалуется на полученный заказ: брак, запах, "
                        "не тот товар, помятая упаковка. Обычный вопрос — не передавай"
                    ),
                },
            },
            "required": ["question", "reason"],
        },
    },
]


TOOLS.append(repeat_one_tap.TOOL)


@dataclass
class ToolExecution:
    # tool_result идёт обратно в Claude, чтобы модель сформулировала ответ.
    # client_reply — готовый ответ клиенту напрямую, без второго вызова
    # модели; заполняется только теми тулами, где текст полностью
    # детерминирован и не зависит от остального контекста реплики.
    tool_result: str
    client_reply: str | None = None


def _tools_for_stage(
    stage: str | None,
    *,
    with_feedback: bool = False,
    live_invoice: bool = False,
    with_offer: bool = False,
    repeatable: bool = False,
) -> list[dict]:
    # Даём модели только те инструменты, которые уместны на текущем этапе —
    # так она физически не может вызвать propose_order повторно, пока черновик
    # ждёт выбора доставки или подтверждения. escalate_to_manager доступен
    # всегда: эскалация может понадобиться на любом шаге диалога.
    #
    # set_recipient идёт рядом со своим этапом, а не отдельным шагом: клиент
    # называет ФИО и телефон когда ему удобно, и модель должна успеть записать
    # их и сразу подтвердить заказ за один ход.
    #
    # set_delivery_method доступен и на подтверждении. Сам инструмент это
    # всегда умел — клиент передумывает, и цена пересчитывается, — а вот
    # выдавали мы его только на выборе доставки. Из-за этого клиент, который
    # после расчёта СДЭКа написал «отмена, буду через Ozon», упёрся в бота
    # без подходящего инструмента: тот честно позвал менеджера с формулировкой
    # «не хватает инструмента для расчёта».
    by_name = {tool["name"]: tool for tool in TOOLS}
    if stage == "awaiting_delivery":
        stage_tools = [
            by_name["set_delivery_method"], by_name["set_recipient"], by_name["add_to_order"],
        ]
    elif stage == "awaiting_confirmation":
        stage_tools = [
            by_name["set_delivery_method"],
            by_name["set_recipient"],
            by_name["confirm_order"],
            by_name["add_to_order"],
        ]
    elif live_invoice:
        # Ссылка выставлена, черновика нет — но клиент вправе поправить
        # заказ: инструменты сами вернут черновик из заказа.
        stage_tools = [
            by_name["propose_order"],
            by_name["set_delivery_method"],
            by_name["set_recipient"],
            by_name["add_to_order"],
        ]
    else:
        stage_tools = [by_name["propose_order"]]
    # cancel_order доступен всегда: неоплаченный заказ живёт и без черновика
    # (счёт выставлен — черновик убран), а отменить его клиент вправе на
    # любом шаге.
    if with_offer:
        stage_tools.append(by_name["accept_offer"])
    if repeatable and stage is None and not live_invoice and "repeat_order" in by_name:
        stage_tools.append(by_name["repeat_order"])
    tools = stage_tools + [by_name["cancel_order"], by_name["escalate_to_manager"]]
    # Отзыв — только когда есть недавно вручённый заказ: иначе модель
    # записывала бы в отзывы любое «спасибо».
    if with_feedback:
        tools.append(feedback.TOOL)
    return tools


def _find_catalog_item(catalog: list[dict], wanted_name: str) -> dict | None:
    wanted_lower = wanted_name.strip().lower()
    for item in catalog:
        item_lower = item["name"].strip().lower()
        if wanted_lower in item_lower or item_lower in wanted_lower:
            return item
    return None


def _load_tariffs() -> dict:
    with _TARIFFS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _apply_free_delivery(draft: OrderDraft) -> str:
    """Порог бесплатной доставки: доставка клиенту 0, настоящая цена — в деталях.

    Настоящую стоимость у перевозчика храним: платит её магазин, и менеджеру
    с отчётами она нужна. В чек ЮKassa позиция доставки с нулём не попадает —
    сумма чека должна сходиться с платежом, а нулевых позиций касса не берёт.
    Возвращает строку для модели (или пустую).
    """
    threshold = free_delivery_threshold()
    if draft.delivery_cost is None or threshold is None:
        return ""
    carrier_cost = draft.details.get("carrier_delivery_cost", draft.delivery_cost)
    if draft.items_total >= threshold:
        draft.details["carrier_delivery_cost"] = carrier_cost
        # Порог покрывает самый дешёвый вариант; за перевозчика дороже
        # клиент доплачивает разницу (app/modules/orders/upgrade.py).
        extra = upgrade.surcharge(draft.details, draft.delivery_method, carrier_cost)
        if extra:
            draft.details[upgrade.SURCHARGE] = extra
            draft.delivery_cost = extra
            base = upgrade.cheapest(upgrade.quotes_for(draft.details))
            return (
                f"Сумма товаров от {templates.amount(threshold)} ₽: самая дешёвая доставка "
                f"({base['name']}) для клиента бесплатная, за выбранную клиент доплачивает "
                f"{templates.amount(extra)} ₽. Скажи именно так: «с доплатой {templates.amount(extra)} ₽».\n"
            )
        draft.details.pop(upgrade.SURCHARGE, None)
        draft.delivery_cost = 0
        return (
            f"Доставка для клиента бесплатная: сумма товаров от "
            f"{templates.amount(threshold)} ₽. Скажи об этом клиенту.\n"
        )
    # Сумма опустилась ниже порога (клиент убрал позицию) — снова платная.
    draft.details.pop(upgrade.SURCHARGE, None)
    if "carrier_delivery_cost" in draft.details:
        draft.delivery_cost = draft.details.pop("carrier_delivery_cost")
    return ""


def _first_sentence(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    for mark in (". ", "! ", "? "):
        if mark in text:
            text = text.split(mark, 1)[0] + mark.strip()
            break
    return text[:limit]


# Откуда пришёл черновик, если его создаёт не реплика клиента: «Взять»,
# «Повторить». Ставит вызывающий на время вызова propose_order.
_draft_origin: contextvars.ContextVar[str | None] = contextvars.ContextVar("draft_origin", default=None)


def _offer_upsell(draft: OrderDraft, catalog: list[dict]) -> str:
    """Подсказка модели: что предложить дополнительно и сколько до порога.

    Позицию выбирает код, а не модель: только из «С чем советуем» у товаров
    черновика. Нет подсказки — нет допродажи. Предлагаем один раз на
    черновик: отметка `upsell_offered` гасит подсказку навсегда.
    """
    lines = []
    if not draft.details.get("upsell_offered"):
        candidate = catalog_service.upsell_for(draft.items, catalog)
        if candidate is not None:
            draft.details["upsell_offered"] = True
            draft.details["upsell_item"] = candidate["name"]
            reason = _first_sentence(candidate.get("description", ""))
            lines.append(
                f"Предложи дополнить: {candidate['name']} ({candidate['price']} ₽) — "
                + (f"причина из описания товара: «{reason}»" if reason else "одной фразой, почему")
                + ". Один раз, в том же сообщении, где спрашиваешь город. Согласится — "
                "вызови add_to_order."
            )
    gap = _threshold_gap(draft.items_total)
    if gap is not None:
        lines.append(
            f"До бесплатной доставки не хватает {templates.amount(gap)} ₽ — скажи "
            "об этом в том же предложении, где предлагаешь дополнить."
        )
    return "".join(f"\n{line}" for line in lines)


def _threshold_gap(items_total: float) -> float | None:
    """Сколько не хватает до бесплатной доставки. None — порога нет или он пройден."""
    threshold = free_delivery_threshold()
    if threshold is None or items_total >= threshold:
        return None
    return round(threshold - items_total, 2)


def _describe_draft(draft: OrderDraft | None) -> str:
    if draft is None:
        return "Активного черновика заказа у клиента нет."

    lines = [f"Черновик заказа на этапе «{draft.stage}»:"]
    if draft.details.get("repeat_note"):
        lines.append(draft.details["repeat_note"])
    for item in draft.items:
        lines.append(f"- {item['name']} x{item['quantity']} = {item['price'] * item['quantity']} ₽")
    lines.append(f"Сумма товаров: {draft.items_total} ₽")
    gap = _threshold_gap(draft.items_total)
    if gap is not None:
        lines.append(f"До бесплатной доставки не хватает {templates.amount(gap)} ₽.")
    elif draft.details.get("carrier_delivery_cost") is not None:
        lines.append("Доставка для клиента бесплатная: сумма товаров прошла порог.")
    if draft.delivery_label:
        lines.append(f"Способ доставки: {draft.delivery_label}, стоимость {draft.delivery_cost} ₽")
        when = eta.phrase(draft.details)
        if when:
            lines.append(f"Срок доставки (называй только так): {when}")

    quotes = draft.details.get("storefront_quotes")
    if quotes and not draft.delivery_method:
        offered = "; ".join(
            f"{q['carrier']} ({q['method']}) — {templates.amount(q['client_cost'])} ₽, {q.get('eta_phrase') or 'срок не известен'}"
            for q in quotes
        )
        lines.append(
            f"Заказ из «Товаров»: клиенту предложены кнопками варианты доставки в "
            f"{draft.details.get('storefront_city')}: {offered}. Выберет словами — вызови "
            f"set_delivery_method с этим method, address=«{draft.details.get('storefront_city')}»"
            + (f", pickup_point=«{draft.details['storefront_street']}»" if draft.details.get("storefront_street") else "")
            + ". Почту проси прямо: она нужна, чтобы отправить чек об оплате."
        )

    candidate = draft.details.get("storefront_recipient")
    if candidate and not draft.details.get("recipient_name"):
        lines.append(
            f"Получатель из заказа витрины (показан клиенту для проверки): {candidate['name']}, "
            f"{candidate['phone']}. Пришлёт только почту — вызови set_recipient с этими ФИО и "
            "телефоном и его почтой; назовёт другого получателя — запиши его."
        )

    # Показываем, что записано на самом деле. Без этого модель судит по
    # собственной прошлой реплике: написала клиенту «получатель записан», а
    # инструмент не вызвала — и узнаёт об этом только при подтверждении.
    name = draft.details.get("recipient_name")
    phone = draft.details.get("recipient_phone")
    email = draft.details.get("recipient_email")
    if name and phone:
        lines.append(f"Получатель записан: {name}, {phone}.")
        if payment_service.is_enabled():
            lines.append(
                f"Почта для чека: {email}." if email
                else "Почта для чека ещё НЕ записана — без неё оплату не выставить."
            )
    elif draft.delivery_method in ("cdek_pvz", "cdek_courier", "ozon_pvz"):
        lines.append(
            "Получатель ещё НЕ записан. Если клиент уже называл ФИО и телефон — "
            "вызови set_recipient с ними, не переспрашивая."
        )

    if draft.delivery_method == "cdek_pvz" and not draft.details.get("delivery_point"):
        lines.append("Пункт выдачи ещё НЕ выбран.")

    if draft.delivery_method == "ozon_pvz" and not draft.details.get("ozon_point_id"):
        lines.append("Пункт выдачи Ozon ещё НЕ выбран.")

    offer = draft.details.get("offer")
    if offer:
        lines.append(
            "Клиенту показано одним сообщением предложение «как в прошлый раз»: "
            f"{offer.get('method')}, пункт «{offer.get('point_address')}» (город {offer.get('city')}), "
            f"получатель {offer.get('name')}, {offer.get('phone')}, {offer.get('email')}. "
            "Согласие («да», «оформляйте») — вызови accept_offer, счёт придёт сам. Хочет "
            "поменять пункт — set_delivery_method (прошлый пункт в списке под номером 1), "
            "получателя — set_recipient; остальное из предложения предлагай как есть."
        )

    shown = draft.details.get("shown_points") or []
    fixed_point = draft.details.get("ozon_point_id") or draft.details.get("delivery_point")
    if draft.details.get("point_asked") and not shown and not fixed_point:
        lines.append(
            f"Пункт выдачи не выбран: клиента попросили назвать улицу и дом пункта, "
            f"адрес с карты или отправить геопозицию. Его ответ-адрес передай в "
            f"pickup_point set_delivery_method с городом "
            f"«{draft.details.get('address', '')}»."
        )
    if shown and not offer and not fixed_point:
        lines.append(
            f"Клиенту показаны пункты: {points.listing(shown)}. Выбор номером или адресом "
            "передай в pickup_point set_delivery_method."
        )

    return "\n".join(lines)


def _describe_live_invoice(order) -> str:
    details = order.details or {}
    return (
        f"По заказу №{order.id} ссылка на оплату уже выставлена и ждёт оплаты: "
        f"{templates.composition(order.items or [])}, доставка — "
        f"{templates.delivery_place(order.delivery_method, details.get('delivery_label'))}, "
        f"получатель {details.get('recipient_name', '—')}, итого {templates.amount(order.total)} ₽. "
        "Если клиент хочет поменять состав, пункт выдачи или получателя — вызывай "
        "обычные инструменты (add_to_order, propose_order с новым составом, "
        "set_delivery_method, set_recipient): код закроет старую ссылку и пришлёт "
        "новую, номер заказа останется тем же. Если клиент просто спрашивает — ответь."
    )


async def _describe_escalation(peer_id: int) -> str:
    if not await escalation_state.is_open(peer_id):
        return ""

    # Формулировки намеренно без слова «эскалация» и прочих внутренних
    # терминов: модель охотно пересказывает их клиенту дословно, и он читает
    # в ответе «эскалация уже открыта», не понимая, о чём речь.
    pending = await escalation_log.get_latest_open(peer_id)
    if pending is None:
        return "Вопрос клиента уже передан менеджеру и ждёт его ответа."

    return (
        "Вопрос клиента уже передан менеджеру и ждёт его ответа.\n"
        f"Что передано: {pending.question} (почему: {pending.reason})"
    )


async def _execute_propose_order(peer_id: int, tool_input: dict) -> str | ToolExecution:
    catalog = catalog_service.load_items()
    resolved, unresolved = [], []

    for wanted in tool_input.get("items", []):
        match = _find_catalog_item(catalog, wanted["name"])
        if match and match.get("in_stock", True):
            resolved.append({"name": match["name"], "quantity": wanted["quantity"], "price": match["price"]})
        else:
            unresolved.append(wanted["name"])

    if not resolved:
        return f"Не нашли в ассортименте товар(ы): {', '.join(unresolved)}. Уточни у клиента точное название."

    items_total = sum(i["price"] * i["quantity"] for i in resolved)
    draft = OrderDraft(items=resolved, items_total=items_total, stage="awaiting_delivery")
    recalc = ""
    live = None
    if await state.get_draft(peer_id) is None:
        live = await orders_repository.live_invoice_order(peer_id)
    if live is not None:
        # Новый состав к заказу, по которому уже выставлена ссылка: номер тот
        # же, получатель тот же, а доставку надо пересчитать — вес другой.
        keep = ("order_id", "order_key", "recipient_name", "recipient_phone", "recipient_email", "address")
        previous = dict(live.details or {})
        previous["order_id"] = live.id
        draft.details.update({key: previous[key] for key in keep if previous.get(key)})
        point = previous.get("ozon_point_address") or (
            (previous.get("delivery_label") or "").split(": ", 1)[1]
            if ": " in (previous.get("delivery_label") or "") else ""
        )
        point_id = previous.get("ozon_point_id") or previous.get("delivery_point")
        if point and point_id:
            points.remember(draft.details, live.delivery_method, previous.get("address", ""),
                            [{"id": point_id, "address": point}])
            point = "1"
        recalc = (
            f"\nЭто новый состав заказа №{live.id}: ссылка по нему уже выставлена, "
            "код закроет её и пришлёт новую. Посчитай доставку заново: вызови "
            f"set_delivery_method с method={live.delivery_method}, "
            f"address=«{previous.get('address', '')}»"
            + (f", pickup_point=«{point}»" if point else "")
            + " — клиенту переспрашивать не нужно."
        )
    was_draft = live is not None or await state.get_draft(peer_id) is not None
    upsell_line = _offer_upsell(draft, catalog)
    origin = None
    if not was_draft:
        # Откуда черновик: «беру» словами, «Взять», «Повторить», витрина —
        # источник ставит тот, кто вызвал (кнопка, повтор), иначе — текст.
        origin = _draft_origin.get()
        if origin is None:
            # Постоянный клиент — отдельный канал: у него свой короткий путь.
            origin = "returning" if await repeat_delivery.last_for(peer_id) is not None else funnel.current_source()
        # В деталях — для канала заказа в аналитике: детали переходят в заказ.
        draft.details["origin"] = origin
    await state.set_draft(peer_id, draft)
    if origin is not None:
        await funnel.record(peer_id, "draft_created", origin=origin)
    if draft.details.get("upsell_item"):
        await funnel.record(peer_id, "upsell_offered", source_=funnel.CODE, item=draft.details["upsell_item"])

    lines = [f"{i['name']} x{i['quantity']} = {i['price'] * i['quantity']} ₽" for i in resolved]
    result = "Черновик заказа создан:\n" + "\n".join(lines) + f"\nСумма товаров: {items_total} ₽"
    if unresolved:
        result += f"\nНе нашли в ассортименте: {', '.join(unresolved)} — уточни у клиента точное название."
    result += upsell_line
    if recalc:
        return result + recalc
    # Пункт выдачи называем первым и объясняем почему: клиенту проще
    # согласиться на вариант, который уже предложен, чем выбирать из списка.
    # Первым идёт Ozon: на живом расчёте он вышел 121 руб против 397 у
    # СДЭКа. Платит за доставку клиент, так что порядок — это про то, какую
    # цену он увидит первой, а не про нашу выгоду.
    # Постоянному клиенту первым предлагаем то, куда он уже получал: одно
    # «да» вместо города, пункта и карты.
    last = await repeat_delivery.last_for(peer_id)
    delivery_hint = str(tool_input.get("delivery_hint") or "").strip()
    recipient_hint = str(tool_input.get("recipient_hint") or "").strip()
    if last is not None and (delivery_hint or recipient_hint):
        # Клиент сам назвал другую доставку или получателя — «как в прошлый
        # раз» не к месту: предложение и счёт по прошлым данным ушли бы
        # мимо того, что он только что написал.
        if delivery_hint:
            return result + (
                f"\nКлиент постоянный, но сейчас назвал, куда везти: «{delivery_hint}». "
                "Прошлую доставку не предлагай — вызови set_delivery_method с этим "
                "городом в address и улицей или пунктом, если он их назвал, в "
                "pickup_point (первым — пункт выдачи Ozon)."
                + (f" Получатель — новый: «{recipient_hint}», попроси недостающие "
                   "ФИО, телефон и почту." if recipient_hint else "")
            )
        last.remember(draft.details)
        await state.set_draft(peer_id, draft)
        return result + "\n" + repeat_delivery.suggestion(last) + (
            f" Получатель в этот раз другой: «{recipient_hint}» — прошлого не предлагай, "
            "попроси ФИО, телефон и почту нового."
        )
    if last is not None:
        offered = await _offer_as_last_time(peer_id, draft, last)
        if offered is not None:
            return offered
        last.remember(draft.details)
        await state.set_draft(peer_id, draft)
        return result + "\n" + repeat_delivery.suggestion(last)
    from app.modules.orders import geo

    with_geo = await geo.offer_for(peer_id)
    result += (
        "\nТеперь предложи клиенту доставку. Первым предлагай пункт выдачи "
        "Ozon — он заметно дешевле, клиент забирает посылку сам. Если нужно "
        "быстрее, предложи пункт выдачи СДЭК: дороже, но идёт в полтора-два "
        "раза меньше. Курьер СДЭК до двери — тоже можно. "
        f"{_OTHER_METHODS_HINT}"
        f"Спроси сразу город и улицу — «{templates.ask_where(with_geo)}»"
        + (" (кнопку геопозиции поставит код; геопозицию разберёт тоже код)" if with_geo
           else " (вместе со ссылкой на карту)")
        + " — в том же сообщении, что и предложение дополнить заказ: по "
        "улице первыми покажутся ближайшие пункты. Назвал только город — "
        "работай с ним, улицу не переспрашивай."
    )
    return result


def _draft_weight_grams(draft: OrderDraft) -> int:
    return shipping.weight_grams(draft.items)


async def _cdek_delivery(
    draft: OrderDraft, method: str, address: str, delivery_point: str | None = None
) -> tuple[cdek_client.Tariff, float]:
    """Тариф СДЭКа и то, во сколько он обойдётся на самом деле.

    Режимы различаем осознанно: посылку мы сами сдаём в отделение, поэтому
    берём тарифы, которые начинаются со «склада». Тариф «до двери» дороже
    «до пункта выдачи» примерно на 250 руб — перепутать их значит возить
    часть заказов себе в убыток.

    Цену берём не из списка тарифов: там `delivery_sum` — база без НДС и
    допсборов. Счёт приходит другой (320 руб базы → 397.72 руб счёта), и
    разницу оплачивал бы магазин.
    """
    modes = cdek_client.TO_DOOR if method == "cdek_courier" else cdek_client.TO_PICKUP
    weight = _draft_weight_grams(draft)
    tariffs = await cdek_client.calculate_tariffs(address, weight)
    tariff = cdek_client.cheapest(tariffs, modes)
    if tariff is None:
        raise cdek_client.CdekError(f"нет подходящего тарифа до «{address}»")

    total = await cdek_client.calculate_total(
        tariff.code,
        address,
        weight,
        declared_value=draft.items_total,
        delivery_point=delivery_point,
    )
    return tariff, total


async def _ozon_points(
    draft: OrderDraft, city: str, hint: str = ""
) -> ozon_quote.Picked:
    """Пункты Ozon под то, что назвал клиент, и счётчики вокруг них."""
    return await ozon_quote.points_for(
        city,
        hint,
        weight_grams=_draft_weight_grams(draft),
        declared_value=draft.items_total,
    )


async def _ozon_price(draft: OrderDraft, point_id: int) -> ozon_client.Quote:
    """Во что обойдётся доставка Ozon в конкретный пункт.

    Телефон Ozon требует и для расчёта, но цена от него не зависит, поэтому
    считаем всегда со служебным. Раньше брали телефон получателя, если он уже
    записан, — и черновик, восстановленный из старого заказа, приносил номер
    в том виде, как его когда-то написали («8921…», со скобками). Ozon такой
    отвергал по всем пунктам, и клиент слышал «расчёт Ozon недоступен»
    (01.10.2026), хотя из контейнера тот же пункт считался. Настоящий телефон
    уходит в Ozon только при создании отправления — и там он нормализуется.
    """
    return await ozon_quote.price_for(
        point_id,
        phone="",
        weight_grams=_draft_weight_grams(draft),
        declared_value=draft.items_total,
    )


async def _execute_set_delivery_method(peer_id: int, tool_input: dict) -> ToolExecution:
    draft = await _draft_for_edit(peer_id)
    # Способ доставки можно уточнять и после того, как цена названа: клиент
    # передумывает, а адрес пункта выдачи приходит отдельным сообщением.
    if draft is None or draft.stage not in ("awaiting_delivery", "awaiting_confirmation"):
        return ToolExecution(
            "Нет черновика заказа, ожидающего выбора доставки. Уточни у клиента, что он хочет заказать."
        )

    method = tool_input.get("method")
    # Схема инструмента уже ограничивает выбор, но модель может назвать способ
    # и мимо неё — а исполнитель до сих пор брал бы его из файла тарифов и
    # спокойно оформил доставку, которой у нас нет.
    if method not in DELIVERY_METHODS:
        return ToolExecution(
            f"Способом «{method}» мы сейчас не отправляем. Скажи об этом клиенту "
            "и предложи пункт выдачи Ozon или СДЭК."
        )

    # Настоящая цена прошлого перевозчика к новому расчёту отношения не
    # имеет: раньше её снимал только Ozon, и СДЭК после Ozon выше порога
    # записывал себе цену Ozon.
    draft.details.pop("carrier_delivery_cost", None)
    # Список пунктов, который увидит клиент, — пока пункт не выбран.
    shown: list[dict] = []
    per_point_prices = False
    not_found_note = ""
    city = ""
    # Геопозиция клиента — только из кода (app/modules/orders/geo.py), не от
    # модели: ближайшие пункты в радиусе вместо поиска по улице.
    near = tool_input.get("_near")
    distances: dict = {}
    # Ровно один пункт на названной улице: (улица, адрес пункта).
    single_point: tuple[str, str] | None = None
    # Город большой, а улица не названа (или не нашлась): список не
    # показываем, просим адрес пункта. Четыре случайных пункта из сотни —
    # не выбор: клиент всё равно идёт на карту, а потом пишет адрес.
    ask_point = False

    if method in ("cdek_pvz", "cdek_courier"):
        address = (tool_input.get("address") or "").strip()
        if not address:
            return ToolExecution(
                "Чтобы посчитать доставку СДЭКом, нужен город клиента "
                "(для курьера — полный адрес). Спроси и вызови инструмент ещё раз."
            )
        city = address
        hint = (tool_input.get("pickup_point") or "").strip() if method == "cdek_pvz" else ""
        # Выбор из показанного списка — номером, кнопкой или адресом — кодом,
        # а не по памяти модели.
        chosen = points.choose(hint, points.shown_for(draft.details, method, address)) if hint else None
        try:
            tariff, total = await _cdek_delivery(
                draft, method, address, delivery_point=chosen["id"] if chosen else None
            )
        except Exception:
            # Адрес в лог не пишем: у курьерской доставки это адрес клиента.
            logger.exception("Не посчитали доставку СДЭК для peer_id=%s (%s)", peer_id, method)
            return ToolExecution(
                "Расчёт СДЭКа сейчас недоступен. Скажи клиенту, что стоимость "
                "доставки уточнит менеджер, и вызови escalate_to_manager."
            )

        eta.remember(draft.details, carrier="cdek", days_min=tariff.period_min,
                     days_max=tariff.period_max, working=True)
        label = "СДЭК, курьером до адреса" if method == "cdek_courier" else "СДЭК, пункт выдачи"
        draft.details["tariff_code"] = tariff.code
        draft.details["address"] = address

        if method == "cdek_pvz" and near and not chosen:
            draft.details.pop("delivery_point", None)
            try:
                city_list = await cdek_client.city_points(address)
            except Exception:
                logger.exception("Не нашли пункты выдачи СДЭК для peer_id=%s", peer_id)
                city_list = []
            measured = sorted(
                ((point, ozon_catalog.distance_m(near[0], near[1], point.latitude, point.longitude))
                 for point in city_list if point.latitude is not None and point.longitude is not None),
                key=lambda pair: pair[1],
            )
            measured = [pair for pair in measured if pair[1] <= settings.geo_search_radius_km * 1000]
            if not measured:
                return ToolExecution(GEO_NOTHING_NEAR)
            shown = points.remember(
                draft.details, method, address,
                [{"id": point.code, "address": point.describe(), "distance": round(meters)}
                 for point, meters in measured[: points.MAX_SHOWN]],
            )
        elif method == "cdek_pvz" and chosen:
            draft.details["delivery_point"] = chosen["id"]
            label = f"{label}: {chosen['address']}"
            points.forget(draft.details)
        elif method == "cdek_pvz":
            draft.details.pop("delivery_point", None)
            try:
                city_list = await cdek_client.city_points(address)
            except Exception:
                logger.exception("Не нашли пункты выдачи СДЭК для peer_id=%s", peer_id)
                city_list = []
            found = cdek_client.match_points(city_list, hint, limit=points.MAX_SHOWN) if hint else []
            if hint and not found:
                not_found_note = f"Пункт «{hint}» в городе {address} не нашёлся — скажи об этом клиенту. "
            if not city_list:
                return ToolExecution(
                    f"Пунктов выдачи СДЭК в городе {address} не нашлось. Предложи "
                    f"курьера СДЭК или пункт выдачи Ozon, карта пунктов СДЭК: {CDEK_OFFICES_MAP_URL}"
                )
            if _single_on_street(True, len(found), found):
                # На названной улице ровно один пункт — он и есть выбор.
                draft.details["delivery_point"] = found[0].code
                label = f"{label}: {found[0].describe()}"
                points.forget(draft.details)
                single_point = (hint, found[0].describe())
            elif not found and len(city_list) > points.MAX_SHOWN:
                ask_point = True
                points.forget(draft.details)
            else:
                shown = points.remember(
                    draft.details, method, address,
                    [{"id": point.code, "address": point.describe()} for point in (found or city_list)],
                )

        draft.delivery_label = label
        # Именно total: в нём НДС и сбор за объявленную стоимость.
        draft.delivery_cost = total
    elif method == "ozon_pvz":
        city = (tool_input.get("address") or "").strip()
        if not city:
            return ToolExecution(
                "Чтобы посчитать доставку Ozon, нужен город клиента. "
                "Спроси и вызови инструмент ещё раз."
            )
        if not ozon_quote.is_ready():
            return ToolExecution(
                "Доставка Ozon пока не настроена. Предложи клиенту пункт выдачи "
                "СДЭК или курьера СДЭК."
            )

        hint = (tool_input.get("pickup_point") or "").strip()
        chosen = points.choose(hint, points.shown_for(draft.details, method, city)) if hint else None
        if chosen:
            try:
                quote = await _ozon_price(draft, int(chosen["id"]))
            except Exception as error:
                logger.warning(
                    "Не посчитали доставку Ozon в пункт %s для peer_id=%s — %s",
                    chosen["id"], peer_id, error,
                )
                return ToolExecution(
                    f"В пункт «{chosen['address']}» Ozon сейчас не считает доставку. "
                    "Предложи клиенту другой пункт из списка."
                )
            draft.details["ozon_point_id"] = int(chosen["id"])
            draft.details["ozon_point_address"] = chosen["address"]
            draft.delivery_label = f"Ozon, пункт выдачи: {chosen['address']}"
            points.forget(draft.details)
        else:
            try:
                if near:
                    pairs = await ozon_quote.points_near(
                        near[0], near[1], radius_km=settings.geo_search_radius_km,
                        weight_grams=_draft_weight_grams(draft), declared_value=draft.items_total,
                    )
                    distances = {row.id: round(meters) for row, meters in pairs}
                    rows = [row for row, _ in pairs]
                    picked = ozon_quote.Picked(rows, len(rows), len(rows), True)
                    # Город — по ближайшему пункту: клиент его мог и не называть,
                    # а выбор «1» дальше сверяется со списком этого города.
                    parsed = address_parser.city_and_street(rows[0].address) if rows else None
                    if parsed:
                        city = parsed[0]
                else:
                    picked = await _ozon_points(draft, city, hint)
            except Exception:
                logger.exception("Не подобрали пункт Ozon в «%s» для peer_id=%s", city, peer_id)
                picked = ozon_quote.Picked([], 0, 0, False)

            candidates = list(picked.points)[: points.MAX_SHOWN]
            narrowed = bool(hint) and picked.hint_matched
            # Без улицы в большом городе — просим адрес пункта; в маленьком
            # пункты города и есть полный список, его и показываем.
            ask_point = bool(candidates) and not narrowed and not near and picked.total > points.MAX_SHOWN
            if hint and not picked.hint_matched:
                # Адрес с карты Ozon может не найтись у нас: копия каталога
                # неполная. Тупик «не нашёлся» хуже, чем пункты города.
                logger.info("Пункт Ozon «%s» в городе %s не нашёлся", hint, city)
                if candidates:
                    not_found_note = (
                        f"Пункт «{hint}» в нашем списке не нашёлся — скажи об этом клиенту. "
                        + ("" if ask_point else "Предложи выбрать из тех, что есть, или назвать адрес иначе. ")
                    )
                hint = ""

            if not candidates and near:
                return ToolExecution(GEO_NOTHING_NEAR)
            if not candidates:
                if picked.found:
                    return ToolExecution(
                        f"Пункты Ozon в городе {city} есть, но доставку нашим методом "
                        "они не принимают. Предложи клиенту пункт выдачи СДЭК."
                    )
                asked = f"«{hint}» " if hint else ""
                return ToolExecution(
                    f"Пункт выдачи Ozon {asked}в городе {city} не нашёлся. Уточни у "
                    "клиента адрес пункта или предложи доставку СДЭКом."
                )

            quote = None
            if _single_on_street(narrowed, picked.total, candidates):
                # На названной улице ровно один пункт — он и есть выбор
                # клиента: записываем, счёт придёт, как только есть получатель.
                only = candidates[0]
                try:
                    quote = await _ozon_price(draft, int(only.id))
                except Exception as error:
                    logger.warning("Не посчитали единственный пункт Ozon %s для peer_id=%s — %s",
                                   only.id, peer_id, error)
                if quote is not None:
                    draft.details["ozon_point_id"] = int(only.id)
                    draft.details["ozon_point_address"] = only.address
                    draft.delivery_label = f"Ozon, пункт выдачи: {only.address}"
                    points.forget(draft.details)
                    single_point = (hint, only.address)
            if single_point is None:
                # Без списка цена нужна одна — «около», по первому пункту города.
                priced, quote = await _price_ozon_points(
                    draft, candidates[:1] if ask_point else candidates, peer_id
                )
                if quote is None:
                    return ToolExecution(
                        "Расчёт Ozon сейчас недоступен. Предложи клиенту доставку СДЭКом, "
                        "а если он хочет именно Ozon — вызови escalate_to_manager."
                    )
                if ask_point:
                    points.forget(draft.details)
                else:
                    per_point_prices = all(row.get("price") is not None for row in priced)
                    for row in priced:
                        if row["id"] in distances:
                            row["distance"] = distances[row["id"]]
                    shown = points.remember(draft.details, method, city, priced)
                # Пункт не фиксируем, даже если он один в городе: выбирает клиент.
                draft.details.pop("ozon_point_id", None)
                draft.details.pop("ozon_point_address", None)
                draft.delivery_label = "Ozon, пункт выдачи (какой — клиент ещё не выбрал)"

        draft.details["address"] = city
        # Тот же урок, что и с СДЭКом: страховку Ozon выставляет отдельной
        # строкой, и «забыть» её значит доплачивать за клиента.
        draft.details.pop("carrier_delivery_cost", None)
        draft.delivery_cost = quote.total
        eta.remember(draft.details, carrier="ozon", days_min=quote.days, days_max=quote.days,
                     working=False)
    else:
        tariffs = _load_tariffs()
        tariff = tariffs.get(method)
        if tariff is None:
            return ToolExecution(
                f"Неизвестный способ доставки: {method}. Предложи клиенту выбрать из вариантов ещё раз."
            )
        draft.delivery_label = tariff["label"]
        draft.delivery_cost = tariff["price"]
        eta.forget(draft.details)

    if ask_point:
        draft.details["point_asked"] = True
    else:
        draft.details.pop("point_asked", None)
    if single_point is not None:
        # Пункт записан без вопроса: в сводке — «Если пункт не тот — напишите».
        draft.details["single_point"] = True
    else:
        draft.details.pop("single_point", None)
    draft.delivery_method = method
    draft.stage = "awaiting_confirmation"
    try:
        await upgrade.compare(draft, city, method)
    except Exception:
        logger.warning("Не сравнили перевозчиков для peer_id=%s", peer_id, exc_info=True)
    free_note = _apply_free_delivery(draft)
    free_note += upgrade.options_note(draft)
    total = draft.items_total + draft.delivery_cost
    # Когда посчитана цена и какой итог клиент от нас услышал: перед счётом
    # старая цена пересчитывается, а изменившийся итог без вопроса не
    # выставляется.
    draft.details["quoted_at"] = time.time()
    draft.details["seen_total"] = total
    await state.set_draft(peer_id, draft)
    order_ref = draft.details.get("order_id")
    await funnel.record(peer_id, "delivery_quoted", order_id=order_ref, method=method,
                        cost=float(draft.delivery_cost or 0))
    if draft.details.get("ozon_point_id") or draft.details.get("delivery_point") or method == "cdek_courier":
        await funnel.record(peer_id, "point_chosen", order_id=order_ref, method=method)
    extra = upgrade.upgraded_by(draft)
    if extra is not None:
        # Клиент выбрал не самый дешёвый вариант: сколько он стоит сверх него
        # и сколько из этого платит клиент (выше порога — доплата).
        await funnel.record(peer_id, "delivery_upgrade", order_id=order_ref, method=method, extra=extra,
                            surcharge=float(draft.details.get(upgrade.SURCHARGE) or 0))

    # Состав заказа перечисляем прямо здесь. Без него модель писала клиенту
    # «Товары: 800 руб.» — сумму, по которой не проверить, то ли он заказывает.
    items_line = ", ".join(
        f"{item['name']} × {item['quantity']}" for item in draft.items
    ) or "—"

    # Пока пункт не выбран, сумма предварительная: считали её по одному из
    # пунктов города, а цена у Ozon от пункта зависит.
    fixed = "Предварительная стоимость доставки" if shown or ask_point else "Способ доставки зафиксирован"
    head = f"{fixed}: {draft.delivery_label}, {draft.delivery_cost} ₽"
    when = eta.phrase(draft.details)
    head += f", срок {when}\n" if when else ".\n"
    if when:
        # Срок — вместе с ценой и этими словами: в нём уже учтена сборка, а
        # голый срок перевозчика клиент принял бы за срок от оплаты.
        head += "Срок называй вместе с ценой, этой же фразой. "
    head += f"Состав заказа (перечисли клиенту названия и количество, а не "
    head += f"только сумму): {items_line} — {draft.items_total} ₽\n"
    head += f"Итого с доставкой: {total} ₽\n"
    head += free_note

    recipient_ask = await _recipient_ask(peer_id, draft, method)
    if single_point is not None:
        if draft.details.get("recipient_name") and draft.details.get("recipient_email"):
            return ToolExecution(
                head + f"На улице «{single_point[0]}» ровно один пункт — он записан: {single_point[1]}. "
                "Получатель уже есть — счёт со ссылкой код пришлёт сам."
            )
        text = templates.single_point(
            street=single_point[0], carrier="Ozon" if method == "ozon_pvz" else "СДЭК",
            address=single_point[1], delivery_cost=draft.delivery_cost,
            surcharge=bool(draft.details.get(upgrade.SURCHARGE)), when=eta.receive(draft.details.get(eta.KEY)),
            email_only=bool(draft.details.get("storefront_recipient")),
        )
        return ToolExecution(
            head + f"На улице «{single_point[0]}» ровно один пункт — он записан: {single_point[1]}. "
            "Если клиент в этом же сообщении прислал почту (или ФИО, телефон и почту) — сразу вызови "
            "set_recipient, счёт придёт сам. Иначе ответь клиенту дословно, одним сообщением: "
            f"«{text}». Его ответ с данными — согласие с пунктом."
            + ("" if recipient_ask == _ASK_RECIPIENT else
               " Постоянному клиенту вместо просьбы о данных предложи прошлого получателя. " + recipient_ask)
        )
    if ask_point:
        return ToolExecution(
            head + "Назови клиенту состав заказа и эти суммы (доставку — «около»). " + not_found_note
            + _ask_point_instruction(method, city, recipient_ask, await _geo_ok(peer_id, method))
        )
    if shown:
        return ToolExecution(
            head + "Назови клиенту состав заказа и эти суммы. " + not_found_note
            + _points_instruction(method, city, shown, per_point_prices, recipient_ask)
        )

    return ToolExecution(head + "Назови клиенту состав заказа и эти суммы. " + recipient_ask)


async def _recipient_ask(peer_id: int, draft: OrderDraft, method: str) -> str:
    """Что сказать про получателя в том же сообщении, что и доставку."""
    if method not in ("cdek_pvz", "cdek_courier", "ozon_pvz"):
        return "Спроси, готов ли он оформить заказ."
    if draft.details.get("recipient_name") and draft.details.get("recipient_email"):
        return "Получатель уже записан — как только пункт выбран, счёт код пришлёт сам."
    # Постоянному клиенту — прошлый получатель одним «да», а не три вопроса.
    last = await repeat_delivery.last_recipient_for(peer_id)
    if last is not None:
        return "Потом: " + repeat_delivery.recipient_suggestion(last)
    return _ASK_RECIPIENT


_ASK_RECIPIENT = (
    "Попроси одним сообщением ФИО получателя, телефон и почту — как только "
    "запишешь их через set_recipient, счёт со ссылкой код пришлёт сам."
)


def _single_on_street(narrowed: bool, total: int, found: list) -> bool:
    """На названной улице ровно один пункт — и правило включено."""
    return bool(settings.single_point_instant_enabled and narrowed and total == 1 and len(found) == 1)


# Ответ инструмента, когда по геопозиции в радиусе ничего нет: его видит
# только код геопозиции, клиенту отвечает он сам.
GEO_NOTHING_NEAR = "GEO_NOTHING_NEAR"


async def _geo_ok(peer_id: int, method: str) -> bool:
    from app.modules.orders import geo

    return await geo.offer_for(peer_id, method)


def _ask_point_instruction(method: str, city: str, recipient_ask: str, geo: bool = False) -> str:
    carrier = "Ozon" if method == "ozon_pvz" else "СДЭК"
    lines = [
        f"Пункт выдачи ещё НЕ выбран. Пунктов {carrier} в городе {city} много — список "
        "не перечисляй и адреса не придумывай. Попроси этими словами, ссылку не "
        f"обрамляй знаками препинания:\n{templates.ask_point_address(carrier, geo)}\n"
    ]
    if recipient_ask == _ASK_RECIPIENT:
        lines.append(
            "В том же сообщении попроси вместе с пунктом прислать ФИО, телефон и почту — "
            "сразу пришлю счёт."
        )
    else:
        lines.append(recipient_ask)
    lines.append(
        "Когда клиент назовёт улицу и дом или пришлёт адрес с карты — вызови "
        "set_delivery_method с тем же городом и этим адресом в pickup_point: инструмент "
        "покажет пункты рядом с номерами. Скриншоты не проси. Геопозицию клиента "
        "разбирает код: ты увидишь [GEO_n] и уже отправленный список пунктов."
    )
    return " ".join(lines)


def _points_instruction(
    method: str, city: str, shown: list[dict], per_point_prices: bool, recipient_ask: str
) -> str:
    carrier = "Ozon" if method == "ozon_pvz" else "СДЭК"
    lines = [f"Пункты выдачи {carrier} в городе {city}: {points.listing(shown)}."]
    if len(shown) == 1:
        lines.append(
            "Нашёлся один пункт — не считай его выбранным: покажи его и спроси, подходит ли."
        )
    else:
        lines.append(f"Перечисли их клиенту с номерами 1–{len(shown)}.")
    if per_point_prices:
        lines.append("Цена у каждого пункта своя — называй её рядом с адресом.")
    else:
        lines.append(
            "Цена посчитана по одному из пунктов — называй её предварительной («около»): "
            "после выбора пункта она пересчитается."
        )
    if method == "ozon_pvz":
        lines.append(
            "Скажи, что это не весь список: все пункты города — на карте "
            f"{OZON_POINTS_MAP_URL}."
        )
    else:
        lines.append(f"Все пункты — на карте {CDEK_OFFICES_MAP_URL}.")
    lines.append("ВАЖНО: пункт ещё НЕ выбран, не говори клиенту, что он выбран.")
    if recipient_ask == _ASK_RECIPIENT:
        lines.append(
            "В том же сообщении попроси: «Выберите пункт и одним сообщением пришлите "
            "ФИО, телефон и почту — сразу пришлю счёт»."
        )
    else:
        lines.append(recipient_ask)
    lines.append(
        "Когда клиент выберет, вызови set_delivery_method с тем же городом и номером "
        "пункта из списка в pickup_point (например «2»); если он в том же сообщении "
        "прислал ФИО, телефон и почту — в том же ходе вызови и set_recipient. Если "
        "клиент назвал другой адрес — передай его в pickup_point, инструмент поищет заново."
    )
    return " ".join(lines)


async def _price_ozon_points(
    draft: OrderDraft, candidates: list, peer_id: int
) -> tuple[list[dict], ozon_client.Quote | None]:
    """Цена доставки в каждый пункт — если Ozon ответит быстро.

    Считаем параллельно: четыре расчёта стоят как один. Не уложились в
    `_PRICE_ALL_SECONDS` — считаем по первому подходящему и называем цену
    «около», как раньше. Пункт, на котором расчёт упал, из списка убираем:
    выбрать его клиент всё равно не сможет (26.09.2026, Краснодар,
    Ставропольская 159 — проверка доступности пропустила, checkout отказал).
    """
    async def price(point):
        return await _ozon_price(draft, point.id)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(price(point) for point in candidates), return_exceptions=True),
            timeout=_PRICE_ALL_SECONDS,
        )
    except asyncio.TimeoutError:
        results = None

    if results is not None:
        rows, first = [], None
        for point, result in zip(candidates, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Не посчитали доставку Ozon в пункт %s для peer_id=%s — %s",
                    point.id, peer_id, result,
                )
                continue
            first = first or result
            rows.append({"id": point.id, "address": point.address, "price": result.total})
        return rows, first

    rows, first = [], None
    for point in candidates:
        if first is None:
            try:
                first = await _ozon_price(draft, point.id)
            except Exception as error:
                logger.warning("Не посчитали доставку Ozon в пункт %s — %s", point.id, error)
                continue
        rows.append({"id": point.id, "address": point.address, "price": None})
    return rows, first


async def _execute_set_recipient(peer_id: int, tool_input: dict) -> str:
    draft = await _draft_for_edit(peer_id)
    if draft is None:
        return "Нет черновика заказа. Уточни у клиента, что он хочет заказать."

    name = (tool_input.get("name") or "").strip()
    phone = (tool_input.get("phone") or "").strip()
    email = (tool_input.get("email") or "").strip()
    if not name or not phone:
        return "Нужны и ФИО получателя, и телефон. Спроси у клиента то, чего не хватает."

    # Телефон проверяем здесь, а не узнаём из отказа ЮKassa: её ошибка
    # приходит на выставлении счёта, когда клиент уже сказал «оформляйте», и
    # выглядит поломкой вместо простого «уточните номер». Храним в одном
    # виде, +7XXXXXXXXXX: клиент пишет через восьмёрку, со скобками, без
    # кода страны, а перевозчик и менеджер должны видеть одно и то же.
    normalized_phone = contacts.normalize_phone(phone)
    if normalized_phone is None:
        return (
            f"Телефон «{phone}» не похож на российский номер: нужны 11 цифр, "
            "как +7 900 123-45-67. Попроси клиента назвать номер целиком и "
            "вызови инструмент ещё раз — остальное уже записано."
        )
    phone = normalized_phone

    # Почта критична: «Чеки от ЮKassa» шлют чек только письмом, и адрес с
    # опечаткой ЮKassa примет — чек уйдёт в никуда. Поэтому проверяем не
    # только вид адреса, но и что домен вообще принимает почту.
    if email:
        checked = await contacts.check_email(email)
        if not checked.ok:
            # Пока клиент не ответил на «может, k@yandex.ru?», счёт сам не
            # выставляется: ссылка ушла бы раньше, чем он проверил почту.
            if checked.suggestion:
                draft.details["email_suggestion"] = checked.suggestion
                # ФИО и телефон с этой попытки — для кнопки «Да, …»: нажатие
                # записывает получателя кодом, не переспрашивая.
                draft.details["pending_recipient"] = {"name": name, "phone": phone}
                await state.set_draft(peer_id, draft)
            hint = (
                f" Возможно, клиент имел в виду {checked.suggestion} — спроси, "
                "так ли это, а не записывай сам."
                if checked.suggestion else ""
            )
            return (
                f"Почта не записана: {checked.problem}.{hint} Попроси клиента "
                "проверить адрес и вызови инструмент снова — ФИО и телефон "
                "передай вместе с ним."
            )
        email = checked.email

    draft.details.pop("email_suggestion", None)
    draft.details.pop("pending_recipient", None)
    draft.details["recipient_name"] = name
    draft.details["recipient_phone"] = phone
    if email:
        draft.details["recipient_email"] = email
    await state.set_draft(peer_id, draft)
    await funnel.record(peer_id, "recipient_set", order_id=draft.details.get("order_id"))

    written = f"Получатель записан: {name}, {phone}"
    written += f", {email}." if email else "."
    if payment_service.is_enabled() and not draft.details.get("recipient_email"):
        # Без почты счёт не выставить: «Чеки от ЮKassa» шлют чек только
        # письмом. Узнать об этом лучше здесь, а не на подтверждении, когда
        # клиент уже сказал «оформляйте».
        return (
            written + " Осталась электронная почта — на неё придёт чек, без "
            "неё оплату не выставить. Спроси её и вызови set_recipient ещё "
            "раз, вместе с ФИО и телефоном."
        )
    return written + (
        " Если пункт выдачи уже выбран, счёт со сводкой код пришлёт сам — "
        "confirm_order не вызывай и «Оформляем?» не спрашивай."
    )


async def _register_in_cdek(peer_id: int, draft: OrderDraft) -> str | None:
    """Завести заказ в СДЭКе. None — если не вышло: заказ доведёт менеджер."""
    registered = await shipping.register(
        peer_id=peer_id,
        delivery_method=draft.delivery_method,
        items=draft.items,
        details=draft.details,
        items_total=draft.items_total,
        delivery_cost=draft.delivery_cost,
    )
    return registered.cdek_uuid


async def _register_in_ozon(peer_id: int, draft: OrderDraft) -> str | None:
    """Завести отправление в Ozon. None — если не вышло: доведёт менеджер."""
    registered = await shipping.register(
        peer_id=peer_id,
        delivery_method=draft.delivery_method,
        items=draft.items,
        details=draft.details,
        items_total=draft.items_total,
        delivery_cost=draft.delivery_cost,
    )
    return registered.ozon_posting


async def _escalate_for_payment(
    peer_id: int, draft: OrderDraft, order_id, reason: str = ""
) -> None:
    """Передать менеджеру заказ, который нужно довести до оплаты.

    Пока кассы нет, бот на этом шаге говорил «ссылка скоро будет» — и на том
    всё заканчивалось: клиент ждал ссылку, которую никто не собирался
    присылать, а менеджер о нём не знал. Поэтому подтверждённый заказ уходит
    обычной эскалацией, той же, что и любой вопрос, которого бот не тянет.

    С включённой кассой сюда попадают только отказы: ЮKassa не ответила или
    отказала, заказ не записался. Причину передаёт вызывающий — менеджеру
    нужен текст ошибки, а не догадка. Раньше здесь стояло «Модуль оплаты не
    подключён» намертво, и после включения кассы человек читал в телеграме
    неправду: касса работает, а сорвался конкретный платёж.

    Эскалацию открываем даже если по этому клиенту уже открыта другая: вопрос
    оплаты не сливается с предыдущим, и потерять его дороже, чем написать
    менеджеру второй раз.
    """
    items = ", ".join(
        f"{item.get('name', 'товар')} × {item.get('quantity', 1)}" for item in draft.items
    )
    total = draft.items_total + (draft.delivery_cost or 0)
    question = (
        f"Заказ {'№' + str(order_id) + ' ' if order_id else ''}на "
        f"{templates.amount(total)} ₽ подтверждён клиентом, счёт не выставился. "
        f"Состав: {items}. Доставка: {draft.delivery_label or '—'}."
    )
    if not reason:
        reason = (
            "Оплата не подключена — ссылку на оплату выставляет менеджер."
            if not payment_service.is_enabled()
            else "Счёт не выставлен, причина не записана — смотреть лог контейнера."
        )

    await escalation_state.mark_open(peer_id)
    try:
        await escalation_log.record_escalation(peer_id, question, reason)
    except Exception:
        logger.exception("Не записали эскалацию по оплате для peer_id=%s", peer_id)

    # Подсказка с командой: у менеджера есть способ выставить счёт самому,
    # и напоминать про него надо ровно там, где он понадобился.
    how = (
        f"\n\nВыставить счёт: <code>scripts/api.sh orders/{order_id}/invoice</code>"
        if order_id
        else "\n\nЗаказ в базу не попал — оформлять вручную."
    )
    message = (
        f"<b>💳 Нужна ссылка на оплату</b>\n{html.escape(question)}\n\n"
        f"{html.escape(reason)}{how}\n\n{vk_client.dialog_link(peer_id)}"
    )
    await _notify_manager(peer_id, message)


# Инструменты, после которых заказ мог стать полным — тогда код сам
# выставляет счёт, без «Оформляем?».
_AUTO_INVOICE_AFTER = {"set_delivery_method", "set_recipient", "add_to_order", "propose_order"}


async def _draft_for_edit(peer_id: int) -> OrderDraft | None:
    """Черновик для правки — или заказ, по которому уже выставлена ссылка.

    Клиент получил ссылку и пишет «поменяйте пункт»: черновика уже нет, он
    убран при выставлении счёта. Возвращаем его из заказа — номер тот же, —
    правка идёт обычными инструментами, а новый счёт закроет старую ссылку.
    """
    draft = await state.get_draft(peer_id)
    if draft is not None:
        return draft
    try:
        live = await orders_repository.live_invoice_order(peer_id)
    except Exception:
        logger.exception("Не проверили выставленный счёт для peer_id=%s", peer_id)
        return None
    if live is None:
        return None
    await payment_service.restore_draft(live)
    return await state.get_draft(peer_id)


def _ready_for_invoice(draft: OrderDraft | None) -> bool:
    """Всё ли есть для счёта: пункт, получатель, почта, телефон целиком."""
    if draft is None or draft.stage != "awaiting_confirmation":
        return False
    details = draft.details
    if not draft.delivery_method or draft.delivery_cost is None:
        return False
    if draft.delivery_method == "ozon_pvz" and not details.get("ozon_point_id"):
        return False
    if draft.delivery_method == "cdek_pvz" and not details.get("delivery_point"):
        return False
    if details.get("email_suggestion"):
        return False
    if not (details.get("recipient_name") and details.get("recipient_email")):
        return False
    return yookassa_client.phone_is_valid(details.get("recipient_phone", ""))


async def _requote(draft: OrderDraft) -> None:
    """Пересчитать доставку по уже выбранному пункту."""
    details = draft.details
    if draft.delivery_method == "ozon_pvz" and details.get("ozon_point_id"):
        quote = await _ozon_price(draft, int(details["ozon_point_id"]))
        cost = quote.total
        eta.remember(details, carrier="ozon", days_min=quote.days, days_max=quote.days, working=False)
    elif draft.delivery_method in ("cdek_pvz", "cdek_courier") and details.get("address"):
        tariff, cost = await _cdek_delivery(
            draft, draft.delivery_method, details["address"],
            delivery_point=details.get("delivery_point"),
        )
        details["tariff_code"] = tariff.code
        eta.remember(details, carrier="cdek", days_min=tariff.period_min,
                     days_max=tariff.period_max, working=True)
    else:
        return
    details.pop("carrier_delivery_cost", None)
    draft.delivery_cost = cost
    details["quoted_at"] = time.time()


async def _refresh_before_invoice(peer_id: int, draft: OrderDraft) -> str | None:
    """Перепроверить цены, наличие и доставку прямо перед счётом.

    None — можно выставлять. Иначе — что сказать модели: товара нет или итог
    изменился, и тогда клиент должен увидеть новую сумму до ссылки.
    """
    catalog = catalog_service.load_items()
    missing = []
    for item in draft.items:
        match = _find_catalog_item(catalog, item["name"])
        if not match or not match.get("in_stock", True):
            missing.append(item["name"])
            continue
        if float(match["price"]) != float(item["price"]):
            # Цену в таблице поменяли, пока клиент выбирал: платит текущую.
            item["price"] = match["price"]
    if missing:
        return (
            f"Счёт не выставлен: {', '.join(missing)} сейчас нет в наличии. Скажи "
            "клиенту и предложи похожее из ассортимента."
        )
    draft.items_total = sum(float(i["price"]) * i["quantity"] for i in draft.items)

    quoted_at = draft.details.get("quoted_at")
    if quoted_at is None or time.time() - float(quoted_at) > settings.delivery_quote_ttl_minutes * 60:
        try:
            await _requote(draft)
            # Доплата считается от самого дешёвого — его цена тоже могла уйти.
            await upgrade.refresh(draft)
        except Exception:
            # Пересчёт не удался — остаётся цена, которую клиент уже видел.
            logger.warning("Не пересчитали доставку перед счётом для peer_id=%s", peer_id, exc_info=True)
    _apply_free_delivery(draft)

    total = draft.items_total + (draft.delivery_cost or 0)
    seen = draft.details.get("seen_total")
    draft.details["seen_total"] = total
    await state.set_draft(peer_id, draft)
    if seen is not None and abs(float(seen) - total) >= 0.01:
        return (
            f"Счёт не выставлен: итог изменился — клиент видел {templates.amount(seen)} ₽, "
            f"теперь {templates.amount(total)} ₽ (товары {templates.amount(draft.items_total)} ₽, "
            f"доставка {templates.amount(draft.delivery_cost or 0)} ₽). Покажи клиенту новую "
            "сводку: состав, пункт, получатель, итог — и спроси «Оформляем?». После «да» "
            "вызови confirm_order."
        )
    return None


async def _auto_invoice(
    peer_id: int, source: str = "invoice_auto", style: str = "summary"
) -> ToolExecution | None:
    """Выставить счёт сам, если заказ стал полным. None — ещё не полный."""
    if not (settings.auto_invoice_enabled and payment_service.is_enabled()):
        return None
    draft = await state.get_draft(peer_id)
    if not _ready_for_invoice(draft):
        return None
    note = await _refresh_before_invoice(peer_id, draft)
    if note is not None:
        return ToolExecution(note)
    draft.stage = "confirmed"
    single = bool(draft.details.get("single_point"))
    result = await _confirm_with_payment(peer_id, draft, source=source, style=style)
    if single and result is not None and result.client_reply is not None:
        # Счёт без вопроса «этот пункт подходит?»: на улице он был один.
        await funnel.record(peer_id, "invoice_single_point", source_=funnel.CODE,
                            order_id=draft.details.get("order_id"))
    return result


def _offer_message(draft: OrderDraft, offer) -> str:
    from app.modules.orders import offers

    cost = offers.client_delivery_cost(draft, offer)
    item = draft.details.get("upsell_item")
    match = catalog_service.find_item(item) if item else None
    return templates.returning_offer(
        items=draft.items,
        delivery_method=offer.method,
        delivery_label=offer.label,
        delivery_cost=cost,
        name=offer.name,
        phone=offer.phone,
        email=offer.email,
        total=draft.items_total + cost,
        upsell=item or "",
        upsell_price=match["price"] if match else None,
        gap=_threshold_gap(draft.items_total) if item else None,
        eta=eta.phrase_for(offer.eta),
    )


async def _returning_invoice_reply(peer_id: int, draft: OrderDraft, payment, order_id) -> ToolExecution:
    """Сводка «как в прошлый раз» со ссылкой и кнопками [Оплатить] [Изменить] [Добавить]."""
    from app.messages import keyboard as keyboards
    from app.modules.orders import buttons

    item = draft.details.get("upsell_item")
    match = catalog_service.find_item(item) if item else None
    if match is None or not match.get("in_stock", True) or item in {row["name"] for row in draft.items}:
        item, match = None, None
    total = payment.amount or (draft.items_total + (draft.delivery_cost or 0))
    reply = templates.returning_invoice(
        items=draft.items,
        delivery_method=draft.delivery_method,
        delivery_label=draft.delivery_label,
        delivery_cost=draft.delivery_cost,
        name=draft.details.get("recipient_name", ""),
        phone=draft.details.get("recipient_phone", ""),
        email=draft.details.get("recipient_email", ""),
        total=total,
        link=payment.confirmation_url,
        eta=eta.phrase(draft.details),
        upsell=item or "",
        upsell_price=match["price"] if match else None,
        gap=_threshold_gap(draft.items_total) if item else None,
        button=await keyboards.shows_link_button(peer_id),
    )
    # Ссылка — своим рядом: open_link ВК растягивает на всю ширину.
    rows = [[keyboards.link_button(f"Оплатить {templates.amount(total)} ₽", payment.confirmation_url)]]
    second = [keyboards.text_button("Изменить", {"a": "edit", "o": order_id})]
    if item:
        second.append(keyboards.text_button(
            f"Добавить {item}", {"a": "add_more", "o": order_id, "n": item}, "positive"
        ))
    rows.append(second)
    buttons.stash(peer_id, keyboards.inline(rows))
    return ToolExecution(reply, client_reply=reply)


def _offer_keyboard(draft: OrderDraft) -> dict | None:
    from app.messages import keyboard as keyboards

    version = draft.details.get("version")
    rows = [[
        keyboards.text_button("Оформить", {"a": "offer_ok", "v": version}, "positive"),
        keyboards.text_button("Изменить", {"a": "edit", "v": version}),
    ]]
    item = draft.details.get("upsell_item")
    if item:
        rows.append([keyboards.text_button(f"Добавить {item}", {"a": "add", "v": version})])
    return keyboards.inline(rows)


async def _offer_as_last_time(peer_id: int, draft: OrderDraft, last) -> ToolExecution | None:
    """Постоянному клиенту — весь заказ одним сообщением.

    None — предложение не собралось (флаг выключен, оплата не подключена,
    получателя нет): тогда прежний путь с вопросами через модель. Пункт
    недоступен — модель скажет об этом, предложит получателя и спросит пункт.
    """
    from app.modules.orders import buttons, offers

    if not (settings.returning_one_question_enabled and payment_service.is_enabled()):
        return None
    recipient = await repeat_delivery.last_recipient_for(peer_id)
    offer = await offers.prepare(draft, last, recipient)
    if not offer.point_ok and offer.recipient_ok:
        await state.set_draft(peer_id, draft)
        return ToolExecution(
            f"Черновик создан. Постоянный клиент, но {', '.join(offer.problems)}. Скажи "
            "об этом клиенту одной фразой. Получателя предложи прошлого: "
            + repeat_delivery.recipient_suggestion(recipient)
            + f" Пункт спроси заново: «{templates.ASK_WHERE}»"
        )
    if not offer.ready:
        return None
    if settings.returning_instant_invoice_enabled:
        # Подтверждением служит оплата: всё проверено — сразу счёт, а не
        # «Оформить?» и та же сводка второй раз, уже со ссылкой.
        offers.apply(draft, offer)
        last.remember(draft.details)
        await state.set_draft(peer_id, draft)
        invoiced = await _auto_invoice(peer_id, source="invoice_returning", style="returning")
        if invoiced is not None:
            return invoiced
        # Счёт сам не выставился (оплата выключена, данных не хватило) —
        # прежний путь: предложение с «Оформить».
        draft = await state.get_draft(peer_id)
        if draft is None:
            return None
    draft.details["offer"] = offer.to_details()
    # «Изменить» — прошлый пункт остаётся под номером 1: модель выберет его
    # без нового поиска, если клиент меняет только получателя.
    last.remember(draft.details)
    # Допродажа — в том же сообщении, кнопкой; отдельная кнопка не нужна.
    draft.details["upsell_button_sent"] = True
    await state.set_draft(peer_id, draft)
    text = _offer_message(draft, offer)
    buttons.stash(peer_id, _offer_keyboard(await state.get_draft(peer_id)))
    return ToolExecution(text, client_reply=text)


async def accept_offer(peer_id: int) -> ToolExecution:
    """«Оформить» или «да» на предложение «как в прошлый раз»: сразу счёт."""
    from app.modules.orders import offers

    draft = await state.get_draft(peer_id)
    offer = offers.Offer.from_details(draft.details.get("offer")) if draft else None
    if offer is None:
        return ToolExecution(
            "Предложения «как в прошлый раз» нет — продолжай оформление обычными инструментами."
        )
    offers.apply(draft, offer)
    await state.set_draft(peer_id, draft)
    invoiced = await _auto_invoice(peer_id) if payment_service.is_enabled() else None
    if invoiced is None:
        return ToolExecution(
            "Доставка и получатель записаны, но счёт сам не выставился. Сверь с клиентом "
            "заказ, спроси «Оформляем?» и после «да» вызови confirm_order."
        )
    return invoiced


async def _execute_accept_offer(peer_id: int) -> ToolExecution:
    return await accept_offer(peer_id)


async def _execute_confirm_order(peer_id: int) -> ToolExecution:
    draft = await state.get_draft(peer_id)
    if draft is None or draft.stage != "awaiting_confirmation":
        return ToolExecution(
            "Нет черновика заказа, ожидающего подтверждения. Уточни у клиента, что он хочет заказать."
        )

    # Заказ в пункт выдачи без кода пункта бесполезен: СДЭК его не примет,
    # а менеджер не поймёт, куда везти посылку.
    if draft.delivery_method == "cdek_pvz" and not draft.details.get("delivery_point"):
        return ToolExecution(
            "Перед подтверждением спроси у клиента адрес пункта выдачи СДЭК, куда "
            f"везти заказ. Можешь прислать карту пунктов: {CDEK_OFFICES_MAP_URL}. "
            "Получишь адрес — вызови set_delivery_method с pickup_point, а потом "
            "confirm_order."
        )

    if draft.delivery_method == "ozon_pvz" and not draft.details.get("ozon_point_id"):
        return ToolExecution(
            "Перед подтверждением нужно выбрать пункт выдачи Ozon: вызови "
            "set_delivery_method с городом клиента и адресом пункта в pickup_point."
        )

    is_cdek = draft.delivery_method in ("cdek_pvz", "cdek_courier")
    is_ozon = draft.delivery_method == "ozon_pvz"
    # Перевозчику всё равно, чей он: без ФИО и телефона отправление не завести
    # ни у СДЭКа, ни у Ozon.
    if (is_cdek or is_ozon) and not (
        draft.details.get("recipient_name") and draft.details.get("recipient_phone")
    ):
        return ToolExecution(
            "Для оформления доставки нужны ФИО получателя и телефон. "
            "Если клиент уже называл их в переписке — вызови set_recipient с "
            "этими данными прямо сейчас, не переспрашивая, и потом confirm_order. "
            "Если не называл — спроси."
        )

    if payment_service.is_enabled() and not draft.details.get("recipient_email"):
        return ToolExecution(
            "Для оплаты нужна электронная почта клиента — на неё придёт чек. "
            "Спроси её и вызови set_recipient с ФИО, телефоном и почтой, а "
            "потом confirm_order."
        )

    if payment_service.is_enabled() and not yookassa_client.phone_is_valid(
        draft.details.get("recipient_phone", "")
    ):
        return ToolExecution(
            "Нужен телефон получателя целиком — 11 цифр. Попроси клиента "
            "назвать номер и вызови set_recipient, а потом confirm_order."
        )

    draft.stage = "confirmed"

    if payment_service.is_enabled():
        return await _confirm_with_payment(peer_id, draft)

    cdek_uuid = await _register_in_cdek(peer_id, draft) if is_cdek else None
    ozon_posting = await _register_in_ozon(peer_id, draft) if is_ozon else None

    # Карточку в чат заказов отправляем прямо здесь — всем, кроме заказов
    # СДЭКа. У СДЭКа ответ асинхронный: он говорит «заявку принял», а
    # состоится ли заказ, выясняет сверка по таймеру, она и пишет в чат с
    # номером накладной. У Ozon номер отправления известен сразу, ждать
    # нечего — а заказ, уехавший в чат через неизвестно сколько (или не
    # уехавший вовсе, если тик расписания не отработал), менеджеру
    # бесполезен.
    reported = "confirmed" if is_cdek else order_chat.STATUS_SENT
    order = None
    try:
        order = await orders_repository.save_order(
            peer_id, draft, cdek_uuid, ozon_posting, status=reported
        )
        if not is_cdek:
            await order_chat.send(order, order_chat.card(order))
    except Exception:
        logger.exception("Failed to persist order to database for peer_id=%s", peer_id)

    # Кассы пока нет, поэтому оплату доводит человек — и узнаёт он об этом
    # сразу, а не из отчёта через полчаса.
    await _escalate_for_payment(peer_id, draft, getattr(order, "id", None))
    payment_message = "Менеджер пришлёт ссылку на оплату — я уже передала ему ваш заказ."

    await state.clear_draft(peer_id)

    # Текст подтверждения полностью определён здесь и клиенту его можно
    # отдать как есть. Это снимает второй заход к Claude на самом дорогом
    # ходу диалога — те самые пара секунд, которых не хватало до таймаута VK.
    carrier_failed = (is_cdek and cdek_uuid is None) or (is_ozon and ozon_posting is None)
    if carrier_failed:
        carrier = "СДЭКе" if is_cdek else "Ozon"
        reply = (
            f"Заказ подтверждён, но в {carrier} его завести не удалось — этим займётся "
            f"менеджер. {payment_message}"
        )
    else:
        reply = f"Заказ подтверждён. {payment_message}"
    return ToolExecution(reply, client_reply=reply)


async def _save_unpaid(peer_id: int, draft: OrderDraft, order_id) -> int | None:
    """Сохранить заказ, счёт по которому выставить не удалось.

    Без номера заказа менеджеру нечего выставлять: служебная команда работает
    по номеру. Раньше в этой ветке заказ не сохранялся вовсе — оставались
    только текст эскалации и черновик у клиента.
    """
    try:
        if order_id:
            return int(order_id)
        order = await orders_repository.save_order(
            peer_id, draft, status=payment_service.STATUS_PAYMENT_FAILED
        )
        draft.details["order_id"] = order.id
        await state.set_draft(peer_id, draft)
        return order.id
    except Exception:
        logger.exception("Не сохранили заказ без счёта для peer_id=%s", peer_id)
        return None


async def _confirm_with_payment(
    peer_id: int, draft: OrderDraft, source: str = "invoice_confirmed", style: str = "summary"
) -> ToolExecution:
    """Подтверждение, когда оплата подключена: счёт вместо отправления.

    Отправление у перевозчика здесь НЕ заводится — оно создаётся после
    того, как пришли деньги. Иначе каждый клиент, получивший ссылку и
    передумавший, оставлял бы за собой настоящий заказ в кабинете СДЭКа или
    Ozon, который кто-то должен удалять руками.

    Номер заказа кладём в черновик до обращения к ЮKassa: из него выводится
    ключ идемпотентности, и повторная попытка (очередь принесла событие
    дважды) обязана вернуть тот же счёт, а не выставить второй.
    """
    # Ожидаемая дата получения на момент счёта — для аналитики: сравнить с
    # фактическим вручением. Считается заново при каждой попытке.
    span = eta.dates(draft.details.get(eta.KEY))
    if span:
        draft.details["expected_from"], draft.details["expected_by"] = span[0].isoformat(), span[1].isoformat()
    else:
        draft.details.pop("expected_from", None)
        draft.details.pop("expected_by", None)
    order_key = draft.details.get("order_key")
    if not order_key:
        order_key = f"vk{peer_id}-{int(time.time())}"
        draft.details["order_key"] = order_key
        await state.set_draft(peer_id, draft)

    # Повторное подтверждение после закрытого счёта — это тот же заказ,
    # вторая попытка оплаты. Номер заказа сохраняется, новым становится
    # только ключ идемпотентности, выведенный из номера попытки.
    order_id = draft.details.get("order_id")
    attempt = 1
    if order_id:
        try:
            attempt = await orders_repository.next_attempt(int(order_id))
        except Exception:
            logger.exception("Не узнали номер попытки оплаты заказа %s", order_id)

    try:
        payment = await payment_service.create_payment(draft, order_key, attempt)
    except yookassa_client.YooKassaUnknown as error:
        # Ответа нет, и счёт мог создаться. Повторять нельзя — спишется
        # дважды; выставлять «не получилось» тоже нельзя, это может быть
        # неправдой. Поэтому зовём человека и оставляем черновик как есть.
        logger.exception("ЮKassa не ответила по заказу %s", order_key)
        await _escalate_for_payment(
            peer_id, draft, await _save_unpaid(peer_id, draft, order_id),
            f"ЮKassa не дала точного ответа, счёт мог создаться — проверить в "
            f"кабинете, прежде чем выставлять новый. {str(error)[:300]}",
        )
        # «Сохранён», а не «подтверждён»: без счёта «подтверждён» звучит
        # как «всё готово».
        reply = (
            "Заказ сохранён, но ссылку на оплату сейчас выставить не получилось. "
            "Менеджер пришлёт её сюда — я уже передала ему заказ."
        )
        return ToolExecution(reply, client_reply=reply)
    except Exception as error:
        logger.exception("Не выставили счёт по заказу %s", order_key)
        await _escalate_for_payment(
            peer_id, draft, await _save_unpaid(peer_id, draft, order_id),
            f"Счёт выставить не удалось: {type(error).__name__}: {str(error)[:300]}",
        )
        reply = (
            "Заказ сохранён, но выставить оплату не получилось. Менеджер пришлёт "
            "ссылку сюда — я уже передала ему заказ."
        )
        return ToolExecution(reply, client_reply=reply)

    try:
        order = None
        if order_id:
            order = await orders_repository.reopen_for_payment(
                int(order_id), draft, payment.id, payment.status
            )
        if order is None:
            order = await orders_repository.save_order(
                peer_id,
                draft,
                status=payment_service.STATUS_AWAITING_PAYMENT,
                payment_id=payment.id,
                payment_status=payment.status,
            )
        # Попытку записываем отдельно: по истёкшему счёту клиент всё ещё
        # может заплатить (отменить pending у ЮKassa нельзя), и уведомление
        # по нему должно находить заказ.
        await orders_repository.register_payment(
            order.id, payment.id,
            attempt=attempt, status=payment.status, amount=payment.amount,
            snapshot=orders_repository.snapshot_of(draft),
        )
        # Прежние ссылки по этому заказу больше не наши: клиент поправил
        # заказ. У ЮKassa pending отменить нельзя, так что отметка — наша;
        # если по старой всё же заплатят, поедет оплаченный снимок.
        for row in await orders_repository.open_payments(order.id):
            if row.payment_id != payment.id:
                await orders_repository.close_payment(row.payment_id)
        await funnel.record(peer_id, source, order_id=order.id, attempt=attempt, total=payment.amount)
    except Exception as error:
        # Заказ не записался, но счёт уже выставлен — деньги придут, а следа
        # у нас не будет. Зовём человека, пока клиент ещё в диалоге.
        logger.exception("Не сохранили заказ %s после выставления счёта", order_key)
        await _escalate_for_payment(
            peer_id, draft, None,
            f"Счёт {payment.id} выставлен, но заказ не записался в базу — "
            f"деньги придут, а следа у нас нет. {type(error).__name__}: "
            f"{str(error)[:300]}",
        )

    await state.clear_draft(peer_id)

    from app.messages import keyboard as keyboards
    from app.modules.orders import buttons

    if style == "returning":
        return await _returning_invoice_reply(peer_id, draft, payment, getattr(order, "id", None) or order_id)

    buttons.stash(peer_id, buttons.pay_keyboard(payment.amount, payment.confirmation_url))
    # Не «заказ оформлен»: до оплаты клиент читал это как «всё готово».
    # Сводка целиком — подтверждением теперь служит сама оплата.
    reply = templates.invoice_summary(
        order_id=getattr(order, "id", None) or order_id,
        items=draft.items,
        delivery_method=draft.delivery_method,
        delivery_label=draft.delivery_label,
        delivery_cost=draft.delivery_cost,
        name=draft.details.get("recipient_name", ""),
        phone=draft.details.get("recipient_phone", ""),
        email=draft.details.get("recipient_email", ""),
        total=payment.amount or (draft.items_total + (draft.delivery_cost or 0)),
        link=payment.confirmation_url,
        eta=eta.phrase(draft.details),
        button=await keyboards.shows_link_button(peer_id),
        surcharge=bool(draft.details.get(upgrade.SURCHARGE)),
        point_note=bool(draft.details.get("single_point")),
    )
    return ToolExecution(reply, client_reply=reply)


async def _notify_manager(
    peer_id: int,
    message: str,
    chat_id: str | None = None,
    kind: str = manager_messages.ESCALATION,
) -> bool:
    """Передать уведомление менеджеру через очередь.

    Возвращает, сохранено ли оно. Отправка может не удаться — телеграм
    отвечает не всегда, — но сохранённое уведомление дошлёт следующий тик
    расписания. Раньше здесь была одна попытка с таймаутом в две секунды: не
    успел телеграм — и вопрос клиента исчезал, хотя ему уже сказали
    «уточню у менеджера».
    """
    return await manager_messages.notify(kind, message, peer_id=peer_id, chat_id=chat_id)


async def _execute_escalate_to_manager(peer_id: int, tool_input: dict) -> ToolExecution:
    if await escalation_state.is_open(peer_id):
        return ToolExecution(
            tool_result=(
                "Вопрос этого клиента уже передан менеджеру и ждёт ответа — "
                "уведомлять менеджера второй раз не нужно. Коротко подтверди "
                "клиенту, что менеджер подключится, и не повторяй это в "
                "следующих ответах, если он сам не спросит."
            ),
            client_reply="Менеджер уже видит ваш вопрос и ответит здесь же 🙏",
        )

    question_raw = tool_input.get("question", "")
    reason_raw = tool_input.get("reason", "")
    await funnel.record(peer_id, "escalation_opened", complaint=True if tool_input.get("complaint") is True else None)
    if tool_input.get("complaint") is True:
        # Жалоба на вручённый заказ — в этом цикле повторных касаний нет.
        try:
            await feedback.mark_complaint(peer_id)
        except Exception:
            logger.exception("Не отметили жалобу у peer_id=%s", peer_id)
    question = html.escape(question_raw)
    reason = html.escape(reason_raw)
    dialog_link = vk_client.dialog_link(peer_id)
    message = templates.manager_question(question, reason, dialog_link)

    # Сначала фиксируем эскалацию у себя — это быстро и надёжно, и именно
    # эта запись, а не уведомление, остаётся следом того, что вопрос передан.
    await escalation_state.mark_open(peer_id)

    try:
        await escalation_log.record_escalation(peer_id, question_raw, reason_raw)
    except Exception:
        logger.exception("Failed to record escalation in database for peer_id=%s", peer_id)

    # Уведомление сначала попадает в очередь и только потом уходит в
    # телеграм. Клиенту мы обещаем «уточню у менеджера» после того, как
    # запись сохранена: не ушло сразу — уйдёт со следующим тиком.
    #
    # В фон это отправлять нельзя: на serverless инстанс засыпает сразу
    # после ответа, и задача умирала, не дойдя до сети. В логах не
    # оставалось ни успеха, ни ошибки.
    stored = await _notify_manager(peer_id, message)
    if not stored:
        # База недоступна: обещание всё равно даём — вопрос уже отмечен
        # открытым, и менеджер увидит его в отчёте, — но в логе это
        # критическая запись, а не рядовая.
        logger.critical(
            "Вопрос клиента peer_id=%s нигде не сохранён: база недоступна", peer_id
        )

    return ToolExecution(
        tool_result=(
            "Вопрос зафиксирован и передан менеджеру. Скажи клиенту, что передала "
            "вопрос и менеджер ответит здесь, в этом диалоге."
        ),
        # «Вернусь с ответом» бот не выполняет — отвечает менеджер. Клиенту
        # важнее знать, где ждать ответ.
        client_reply="Передала ваш вопрос менеджеру — он ответит здесь, в этом диалоге 🙏",
    )


async def _execute_add_to_order(peer_id: int, tool_input: dict) -> str:
    """Дополнить состав черновика — минимальный путь для допродажи.

    Раньше состав после propose_order поменять было нельзя вовсе: на этапе
    доставки инструмента для этого не было, а propose_order там недоступен.
    """
    # Состав меняют и после ссылки: номер заказа остаётся, счёт выставится
    # новый, а старая ссылка закроется с нашей стороны.
    draft = await _draft_for_edit(peer_id)
    if draft is None or draft.stage not in ("awaiting_delivery", "awaiting_confirmation"):
        return "Нет черновика заказа. Если клиент хочет купить — вызови propose_order."

    catalog = catalog_service.load_items()
    added, unresolved = [], []
    for wanted in tool_input.get("items", []):
        match = _find_catalog_item(catalog, wanted.get("name", ""))
        quantity = int(wanted.get("quantity") or 1)
        if not match or not match.get("in_stock", True) or quantity < 1:
            unresolved.append(wanted.get("name", ""))
            continue
        for row in draft.items:
            if row["name"] == match["name"]:
                row["quantity"] += quantity
                break
        else:
            draft.items.append({"name": match["name"], "quantity": quantity, "price": match["price"]})
        added.append(f"{match['name']} × {quantity}")

    if not added:
        return (
            f"Не нашли в наличии: {', '.join(unresolved)}. Уточни у клиента название."
        )
    upsell = draft.details.get("upsell_item")
    if upsell and any(item.startswith(f"{upsell} ×") for item in added):
        await funnel.record(peer_id, "upsell_accepted", item=upsell)

    draft.items_total = sum(row["price"] * row["quantity"] for row in draft.items)
    if draft.details.get("offer"):
        # Вес вырос — цена доставки в предложении устарела: пересчитаем
        # перед счётом.
        draft.details["offer"]["quoted_at"] = 0
    recalc = ""
    if draft.delivery_method:
        # Вес и объявленная ценность выросли — прежняя цена доставки неверна.
        previous = {
            "method": draft.delivery_method,
            "city": draft.details.get("address", ""),
            "point": draft.details.get("ozon_point_address")
            or ((draft.delivery_label or "").split(": ", 1)[1] if ": " in (draft.delivery_label or "") else ""),
            "id": draft.details.get("ozon_point_id") or draft.details.get("delivery_point"),
        }
        if previous["id"] and previous["point"]:
            # Выбранный пункт остаётся выбранным: в список под номером 1, и
            # пересчёт сведёт «1» к нему без нового поиска и вопроса.
            points.remember(
                draft.details, previous["method"], previous["city"],
                [{"id": previous["id"], "address": previous["point"]}],
            )
            previous["point"] = "1"
        draft.delivery_method = None
        draft.delivery_label = None
        draft.delivery_cost = None
        for key in ("carrier_delivery_cost", "ozon_point_id", "ozon_point_address", "delivery_point"):
            draft.details.pop(key, None)
        eta.forget(draft.details)
        draft.stage = "awaiting_delivery"
        recalc = (
            "\nДоставку надо посчитать заново: вызови set_delivery_method с "
            f"method={previous['method']}, address=«{previous['city']}»"
            + (f", pickup_point=«{previous['point']}»" if previous["point"] else "")
            + " — клиенту переспрашивать не нужно."
        )
    await state.set_draft(peer_id, draft)

    lines = [f"{row['name']} x{row['quantity']} = {row['price'] * row['quantity']} ₽" for row in draft.items]
    result = (
        f"Добавлено: {', '.join(added)}. Состав теперь:\n" + "\n".join(lines)
        + f"\nСумма товаров: {draft.items_total} ₽"
    )
    if unresolved:
        result += f"\nНе нашли в наличии: {', '.join(unresolved)}."
    gap = _threshold_gap(draft.items_total)
    if gap is not None:
        result += f"\nДо бесплатной доставки не хватает {templates.amount(gap)} ₽."
    elif free_delivery_threshold() is not None:
        result += "\nСумма товаров прошла порог — доставка будет бесплатной."
    return result + recalc


async def _execute_cancel_order(peer_id: int) -> ToolExecution:
    """Отмена по просьбе клиента: всё неоплаченное — сразу, без менеджера.

    Раньше инструмента не было, и на «отмените заказ» бот звал менеджера,
    даже когда отменять было нечего, кроме неоплаченного счёта.
    """
    outcome = await cancellation.cancel_for_client(peer_id)

    # Отменили хоть что-то — просьба клиента выполнена, отвечаем готовым
    # текстом. Про оплаченные заказы здесь молчим: раньше результат
    # дописывал «есть ещё оплаченный — если клиент про него, зови
    # менеджера», и модель на простое «заказ отмени» звала менеджера, хотя
    # неоплаченный заказ уже был отменён. Если клиент имел в виду
    # оплаченный, он скажет, и следующий вызов уйдёт в ветку ниже.
    if outcome.canceled:
        numbers = ", ".join(f"№{n}" for n in outcome.canceled)
        reply = (
            f"Отменила заказ {numbers} — оплачивать его не нужно. Если вы уже "
            "успели заплатить, деньги вернутся автоматически. Захотите "
            "заказать снова — напишите 🙂"
        )
        return ToolExecution(reply, client_reply=reply)
    if outcome.draft_dropped:
        reply = "Хорошо, заказ не оформляю. Если передумаете — напишите 🙂"
        return ToolExecution(reply, client_reply=reply)
    if outcome.paid:
        paid = ", ".join(f"№{n}" for n in outcome.paid)
        return ToolExecution(
            f"Неоплаченных заказов у клиента нет. Заказ {paid} уже оплачен — "
            "отменить его бот не может: нужен возврат денег, а посылка, "
            "возможно, уже в пути. Вызови escalate_to_manager: клиент просит "
            f"отменить оплаченный заказ {paid}."
        )
    return ToolExecution(
        "Отменять нечего: у клиента нет ни черновика, ни неоплаченного "
        "заказа. Уточни у клиента, что он имеет в виду."
    )


async def _execute_tool(peer_id: int, name: str, tool_input: dict) -> ToolExecution:
    if name == "propose_order":
        result = await _execute_propose_order(peer_id, tool_input)
        return result if isinstance(result, ToolExecution) else ToolExecution(result)
    if name == "accept_offer":
        return await _execute_accept_offer(peer_id)
    if name == "repeat_order":
        return await repeat_one_tap.repeat_order(peer_id, int(tool_input.get("order_id") or 0))
    if name == "set_delivery_method":
        return await _execute_set_delivery_method(peer_id, tool_input)
    if name == "confirm_order":
        return await _execute_confirm_order(peer_id)
    if name == "set_recipient":
        return ToolExecution(await _execute_set_recipient(peer_id, tool_input))
    if name == "add_to_order":
        return ToolExecution(await _execute_add_to_order(peer_id, tool_input))
    if name == "cancel_order":
        return await _execute_cancel_order(peer_id)
    if name == "escalate_to_manager":
        return await _execute_escalate_to_manager(peer_id, tool_input)
    if name == "save_feedback":
        return ToolExecution(await feedback.save(peer_id, tool_input))
    return ToolExecution(f"Неизвестный инструмент: {name}")


class _Spent:
    """Куда ушло время внутри хода.

    Общей длительности мало: 13 секунд из-за медленной модели и 13 секунд
    из-за медленного СДЭКа лечатся по-разному, а по одной цифре их не
    различить. Разбираться постфактум в логах дороже, чем считать сразу.
    """

    def __init__(self) -> None:
        self.claude_seconds = 0.0
        self.claude_calls = 0
        self.tool_seconds = 0.0
        self.tool_calls = 0

    async def claude(self, coro):
        started = time.monotonic()
        try:
            return await coro
        finally:
            self.claude_seconds += time.monotonic() - started
            self.claude_calls += 1

    async def tool(self, coro):
        started = time.monotonic()
        try:
            return await coro
        finally:
            self.tool_seconds += time.monotonic() - started
            self.tool_calls += 1

    def describe(self, total: float) -> str:
        other = total - self.claude_seconds - self.tool_seconds
        return (
            f"claude={self.claude_calls}×{self.claude_seconds:.2f}с "
            f"инструменты={self.tool_calls}×{self.tool_seconds:.2f}с "
            f"прочее={other:.2f}с всего={total:.2f}с"
        )


# Что делать с тем, что модель открыть не может. Фотографии она видит сама,
# а голосовое, видео или файл до неё не доедут никогда — и молчать об этом
# хуже всего: клиент решит, что его не услышали, и пришлёт то же ещё раз.
_ATTACHMENT_PROMPT = (
    "Клиент приложил к сообщению вложение, которое ты открыть не можешь "
    "(оно названо в его реплике). Скажи об этом прямо и попроси написать "
    "текстом или прислать фотографию — не делай вид, что ты его посмотрел, "
    "и не угадывай содержимое."
)


# Шаблонный вопрос ВК «как оплатить заказ и когда сможете доставить?» пришёл,
# а сам заказ из «Товаров» — нет (событие задержалось или не дошло).
_STOREFRONT_PENDING = (
    "Клиент прислал шаблонный вопрос ВКонтакте «как оплатить заказ и когда сможете "
    "доставить?» — его отправляют, оформив заказ в разделе «Товары» сообщества. Сам "
    "заказ к нам пока не пришёл. Скажи, что видишь заказ из «Товаров» и сейчас пришлёшь "
    "варианты доставки; если в истории нет состава и города — попроси назвать номер заказа "
    "или что заказано и город. Не говори, что заказа нет или он отменён."
)


# Метки вместо персональных данных (app/privacy). Модель не знает значений,
# и это нормально: код подставит их сам — в ответ клиенту и в инструменты.
_PII_PROMPT = (
    "Персональные данные клиента в переписке заменены метками: [NAME_1] — ФИО, "
    "[PHONE_1] — телефон, [EMAIL_1] — почта, [ADDR_1] — адрес. Это нормально, "
    "данные на месте: код подставит настоящие значения в твой ответ клиенту и "
    "в аргументы инструментов. Поэтому:\n"
    "- используй метки как есть — и в ответах, и в аргументах инструментов: "
    "на «1, [NAME_1], [PHONE_1], [EMAIL_1]» вызывай set_recipient с "
    "name=[NAME_1], phone=[PHONE_1], email=[EMAIL_1];\n"
    "- не угадывай значения и не проси клиента повторить данные, если метка уже "
    "есть: она и есть эти данные;\n"
    "- в сводках и подтверждениях пиши метку («Получатель: [NAME_1], [PHONE_1]») — "
    "клиент увидит настоящие ФИО и телефон;\n"
    "- не придумывай новых меток и не меняй номер в метке."
)


# ВК не показывает разметку: «**Те Гуань Инь**» клиент видит со
# звёздочками (27.09.2026). Модель пишет её по привычке, даже когда просят не
# писать, поэтому снимаем её и в коде. Одиночную звёздочку не трогаем — она
# бывает и в обычном тексте.
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_BULLET = re.compile(r"^(\s*)[*-]\s+", re.M)


_ASKED_NOT_TO_WRITE = (
    "Клиент просит больше ему не писать. Напоминания ему уже отключены. Ответь "
    "коротко и дружелюбно, что поняла и сама больше писать не будешь, а если "
    "понадобится — пусть пишет сюда. Черновик заказа не трогай, инструменты не "
    "вызывай, ничего не уговаривай."
)


async def _with_buttons(peer_id: int, reply: str, *, consult: bool = False) -> str:
    """Кнопки под ответом хода — по тому, чем ход закончился."""
    from app.modules.orders import buttons

    return await buttons.prepare(peer_id, reply, consult=consult)


# После этих инструментов «Взять» под ответом неуместно: вопрос передан
# человеку, и предлагать купить посреди этого — глухота.
_NO_TAKE_AFTER = {"escalate_to_manager"}


def plain_text(text: str) -> str:
    """Ответ без markdown: жирный — обычным текстом, пункты — «•»."""
    if not text:
        return text
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = text.replace("**", "")
    text = _HEADING.sub("", text)
    return _BULLET.sub(r"\1• ", text)


def _for_history(spoken: str, images: list) -> str:
    """Реплика клиента в том виде, в каком она останется в истории.

    Сами снимки не храним: в истории они стоили бы токенов на каждом
    следующем ходу, а для разговора достаточно знать, что фотография была.
    """
    if not images:
        return spoken
    mark = f"[фото: {len(images)} шт.]" if len(images) > 1 else "[фото]"
    return f"{mark} {spoken}".strip()


async def handle_turn(
    peer_id: int,
    user_text: str,
    attached: vk_attachments.Collected | None = None,
    *,
    budget_seconds: float | None = None,
) -> str:
    global _cold_start
    started = time.monotonic()
    spent = _Spent()
    cold = _cold_start
    _cold_start = False
    try:
        return await _handle_turn(peer_id, user_text, spent, attached, budget_seconds)
    finally:
        logger.info(
            "ход peer_id=%s %s%s",
            peer_id,
            spent.describe(time.monotonic() - started),
            " (холодный старт)" if cold else "",
        )


async def _handle_turn(
    peer_id: int,
    user_text: str,
    spent: _Spent,
    attached: vk_attachments.Collected | None = None,
    budget_seconds: float | None = None,
) -> str:
    if (
        not (attached and attached.any)
        and marketing.is_stop_request(user_text)
        and await marketing.answers_sales_reminder(peer_id)
    ):
        # «Стоп» в ответ на напоминание решает код, без модели: отписка
        # должна срабатывать всегда и одинаково. Флаг гасит только продающие
        # напоминания — сообщения по заказам идут, как шли. Во всех прочих
        # случаях «стоп» — обычная реплика: посреди оформления это пауза.
        await marketing.opt_out(peer_id)
        reply = templates.marketing_stopped()
        await dialog_history.append_exchange(peer_id, user_text, reply)
        logger.info("peer_id=%s отписался от напоминаний", peer_id)
        return reply

    # «Отпишите меня», «не пишите» посреди заказа отвечает модель — по-человечески,
    # а код тихо гасит продающие напоминания: «заказ ждёт вас» после такой
    # просьбы был бы ровно тем, о чём просили не делать. Вернётся клиент сам —
    # бот отвечает как обычно.
    stop_note = ""
    if not (attached and attached.any) and marketing.asks_not_to_write(user_text):
        await marketing.opt_out(peer_id)
        logger.info("peer_id=%s попросил не писать — напоминания отключены", peer_id)
        stop_note = _ASKED_NOT_TO_WRITE

    catalog_context = await catalog_service.build_catalog_context()
    draft = await state.get_draft(peer_id)
    storefront_note = ""
    if (
        (draft is None or draft.details.get("vk_order_id"))
        and not (attached and attached.any)
        and orders_service.looks_like_order_question(user_text)
    ):
        # Шаблонный вопрос ВК после заказа в «Товарах» приходит раньше самого
        # заказа: ждём его, иначе модель ответит, не зная про заказ.
        draft = await orders_service.wait_for_order(peer_id)
        order_id = draft.details.get("vk_order_id") if draft else None
        if order_id and await orders_service.offered_recently(order_id):
            # Ответ на вопрос — предложение доставки — уже у клиента.
            await dialog_history.append_message(peer_id, "user", user_text)
            logger.info("peer_id=%s: шаблонный вопрос о заказе %s — ответ уже отправлен", peer_id, order_id)
            return ""
        if draft is None:
            storefront_note = _STOREFRONT_PENDING

    system_prompt = _BASE_SYSTEM_PROMPT
    if privacy.is_enabled():
        system_prompt += f"\n\n{_PII_PROMPT}"
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"
    # Дальше — то, что про этого клиента: перед отправкой его личное станет метками.
    personal_from = len(system_prompt)
    # Что клиент брал и как оценил — тем же способом, что ассортимент. Начало
    # отзыва — слова клиента, в нём ищем и имена.
    bought = await purchases.context(peer_id)
    if bought:
        system_prompt += f"\n\n{await privacy.tokenize(peer_id, bought)}"
    system_prompt += f"\n\n{order_flow_prompt()}"
    system_prompt += f"\n\n{_describe_draft(draft)}"
    live = None
    if draft is None:
        try:
            live = await orders_repository.live_invoice_order(peer_id)
        except Exception:
            logger.exception("Не проверили выставленный счёт для peer_id=%s", peer_id)
    if live is not None:
        system_prompt += "\n" + _describe_live_invoice(live)
    repeatable = None
    if draft is None and live is None and repeat_one_tap.is_enabled():
        try:
            repeatable = await repeat_one_tap.repeatable_order(peer_id)
        except Exception:
            logger.exception("Не нашли заказ для повтора у peer_id=%s", peer_id)
    if repeatable is not None:
        system_prompt += (
            f"\nПоследний удачный заказ клиента — №{repeatable.id}: "
            f"{templates.composition(repeatable.items or [])}. Если клиент хочет повторить "
            f"его («да, давайте так же» на «повторить заказ?») — вызови repeat_order с "
            f"order_id={repeatable.id}: инструмент сам проверит цены, пункт и получателя и "
            "пришлёт счёт."
        )
    if draft is not None and draft.details.get("repeat_note"):
        # Пояснение, почему повтор не выставил счёт, — один раз, этому ходу.
        draft.details.pop("repeat_note")
        await state.set_draft(peer_id, draft)
    if draft is not None and draft.stage == "awaiting_delivery" and not draft.delivery_method:
        # То же предложение, что в ответе propose_order, — и для черновика
        # из витрины ВК, который собирается без propose_order.
        last = await repeat_delivery.last_for(peer_id)
        if last is not None:
            if not draft.details.get("shown_points"):
                last.remember(draft.details)
                await state.set_draft(peer_id, draft)
            system_prompt += f"\n{repeat_delivery.suggestion(last)}"
    elif (
        draft is not None
        and draft.delivery_method in ("cdek_pvz", "cdek_courier", "ozon_pvz")
        and not draft.details.get("recipient_name")
    ):
        last_recipient = await repeat_delivery.last_recipient_for(peer_id)
        if last_recipient is not None:
            system_prompt += f"\n{repeat_delivery.recipient_suggestion(last_recipient)}"

    if attached and attached.notes:
        # Подсказку добавляем только когда есть что объяснять: постоянная
        # строчка про голосовые в промпте — это токены на каждом ходу и
        # лишний повод упомянуть их к месту и не к месту.
        system_prompt += f"\n\n{_ATTACHMENT_PROMPT}"

    if stop_note:
        system_prompt += f"\n\n{stop_note}"
    if storefront_note:
        system_prompt += f"\n\n{storefront_note}"

    escalation_note = await _describe_escalation(peer_id)
    if escalation_note:
        system_prompt += f"\n\n{_ESCALATION_FLOW_PROMPT}\n\n{escalation_note}"

    delivered = await feedback.recent_delivered(peer_id)
    if delivered is not None:
        system_prompt += f"\n\n{feedback.prompt_for(delivered, await feedback.rating_of(delivered.id))}"

    tools = _tools_for_stage(
        draft.stage if draft else None,
        with_feedback=delivered is not None,
        live_invoice=live is not None,
        with_offer=bool(draft and draft.details.get("offer")),
        repeatable=repeatable is not None,
    )

    # Вложения, которые показать нельзя, объясняем словами — и тем же текстом
    # кладём в историю. Иначе следующий ход увидит реплику клиента пустой и
    # не поймёт, о чём был разговор.
    note = vk_attachments.describe(attached) if attached else ""
    spoken = " ".join(part for part in (user_text, note) if part)
    images = list(attached.images) if attached else []

    history = await dialog_history.get_history(peer_id)
    # Метки вместо персональных данных — после склейки, до истории и модели.
    # Этап подсказывает, чего ждём: ФИО получателя или адрес для курьера.
    last_bot = next(
        (m["content"] for m in reversed(history) if m["role"] == "assistant" and isinstance(m["content"], str)), ""
    )
    spoken = await privacy.tokenize(peer_id, spoken, stage=privacy.stages(draft, last_bot))
    system_prompt = system_prompt[:personal_from] + await privacy.tokenize(
        peer_id, system_prompt[personal_from:], names=False
    )
    if images:
        # Снимок идёт перед текстом: так модель сначала смотрит, а потом
        # читает вопрос о том, что увидела.
        content: str | list = [
            *images,
            {"type": "text", "text": spoken or "Клиент прислал фотографию без подписи."},
        ]
    else:
        content = spoken
    messages: list[dict] = history + [{"role": "user", "content": content}]

    turn_started = time.monotonic()
    response = await spent.claude(claude_client.converse(messages, system_prompt, tools))

    called: set[str] = set()
    for round_number in range(1, _MAX_TOOL_ROUNDS + 1):
        if response.stop_reason != "tool_use":
            break

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})

        # Модель пишет метки — инструменту нужны значения: подставляем до
        # проверок почты и телефона и до записи в черновик. Результат обратно
        # модели — снова с метками.
        executions = []
        for block in tool_use_blocks:
            execution = await spent.tool(
                _execute_tool(peer_id, block.name, await privacy.detokenize_data(peer_id, block.input))
            )
            execution.tool_result = await privacy.tokenize(peer_id, execution.tool_result, names=False)
            executions.append((block, execution))
        for block, execution in executions:
            # Без этой строки по логам не понять, почему бот ответил так, а
            # не иначе: видно только время хода. Данные клиента сюда не
            # пишем — только инструмент и начало его результата.
            logger.info(
                "ход peer_id=%s: %s → %s%s",
                peer_id, block.name, execution.tool_result[:160].replace("\n", " "),
                " [готовый ответ]" if execution.client_reply is not None else "",
            )

        # Если у последнего инструмента есть готовый ответ клиенту, отдаём его
        # напрямую. Второй запрос к Claude нужен лишь чтобы пересказать то же
        # самое своими словами, а стоит он несколько секунд — из-за него путь
        # эскалации не укладывался в таймаут вебхука VK: тот рвал соединение
        # (в логах ERROR Code 499), и человек не получал ничего.
        #
        # Смотрим именно на последний инструмент: он и есть итог хода. Когда
        # условие было «ровно один инструмент», ход из set_recipient и
        # confirm_order уходил на пересказ, и модель теряла из готового текста
        # важное — клиент читал «Заказ оформлен! ✅» вместо честного «заказ
        # подтверждён, но в СДЭК не уехал».
        # Заказ стал полным — счёт выставляет код, без «Оформляем?». Не после
        # confirm_order: тот выставил счёт сам.
        names = {block.name for block, _ in executions}
        called |= names
        if (
            executions
            and executions[-1][1].client_reply is None
            and names & _AUTO_INVOICE_AFTER
            and "confirm_order" not in names
        ):
            invoiced = await spent.tool(_auto_invoice(peer_id))
            if invoiced is not None:
                if invoiced.client_reply is not None:
                    executions.append((executions[-1][0], invoiced))
                else:
                    block, execution = executions[-1]
                    executions[-1] = (
                        block, ToolExecution(execution.tool_result + "\n" + invoiced.tool_result)
                    )

        if executions and executions[-1][1].client_reply is not None:
            reply = await _with_buttons(peer_id, plain_text(executions[-1][1].client_reply))
            await dialog_history.append_exchange(peer_id, _for_history(spoken, images), reply)
            return await privacy.detokenize(peer_id, reply)

        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": block.id, "content": execution.tool_result}
                for block, execution in executions
            ],
        })

        # Этап заказа мог смениться прямо сейчас: после set_delivery_method
        # черновик ждёт подтверждения, и confirm_order должен стать доступен
        # в этом же ходу, а не со следующего сообщения клиента.
        fresh_draft = await state.get_draft(peer_id)
        tools = _tools_for_stage(
            fresh_draft.stage if fresh_draft else None,
            with_feedback=delivered is not None,
            live_invoice=fresh_draft is None and live is not None,
            with_offer=bool(fresh_draft and fresh_draft.details.get("offer")),
        )

        # Последний круг зовём без инструментов: модель обязана ответить
        # словами. Раньше здесь просто стояла заглушка «Записала, спасибо»,
        # и клиент, спросивший цену, получал её вместо цены.
        last_round = (
            round_number == _MAX_TOOL_ROUNDS
            or time.monotonic() - turn_started > min(
                _TURN_BUDGET_SECONDS, budget_seconds or _TURN_BUDGET_SECONDS
            )
        )
        if last_round:
            logger.warning(
                "Ход peer_id=%s дошёл до последнего круга (%s-й, %.1fс) — "
                "спрашиваем ответ словами",
                peer_id, round_number, time.monotonic() - turn_started,
            )

        response = await spent.claude(
            claude_client.converse(messages, system_prompt, [] if last_round else tools)
        )
        if last_round:
            break

    text = claude_client.extract_text(response, default=_NO_TEXT_FALLBACK)
    reply = await _with_buttons(
        peer_id, plain_text(text),
        consult=text != _NO_TEXT_FALLBACK and not (called & _NO_TAKE_AFTER),
    )
    # Незнакомая метка — исключение: ход не отвечает, клиент получает
    # «техническую заминку» (dialog/service.py), а не текст с дырой.
    restored = await privacy.detokenize(peer_id, reply)
    await dialog_history.append_exchange(peer_id, _for_history(spoken, images), reply)
    return restored
