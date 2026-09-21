from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.modules.catalog import service as catalog_service
from app.modules.delivery import cdek_client, ozon_client, ozon_quote
from app.modules.dialog import (
    claude_client,
    escalation_log,
    escalation_state,
    vk_client,
    history as dialog_history,
    telegram_client,
)
from app.modules.dialog.claude_client import _BASE_SYSTEM_PROMPT
from app.modules.orders import repository as orders_repository
from app.modules.orders import state
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service

logger = logging.getLogger(__name__)

_ESCALATION_FLOW_PROMPT_PATH = Path(__file__).parent.parent / "dialog" / "prompts" / "escalation_flow_prompt.md"
_ESCALATION_FLOW_PROMPT = _ESCALATION_FLOW_PROMPT_PATH.read_text(encoding="utf-8")

_TARIFFS_PATH = Path(__file__).parent / "delivery_tariffs.json"

# Карта пунктов выдачи: адрес пункта спрашиваем у клиента словами, а ссылку
# даём, чтобы он мог свериться. Тянуть список пунктов из API в путь обработки
# сообщения нельзя — не укладываемся в 8 секунд, которые даёт VK.
CDEK_OFFICES_MAP_URL = "https://www.cdek.ru/ru/offices"

# На случай, когда модель второй раз подряд просит инструмент и не пишет ни
# слова клиенту, а у инструмента нет готового ответа. Лучше нейтральная
# фраза, чем извинение за несуществующую поломку.
_SECOND_ROUND_FALLBACK = "Записала, спасибо! Подскажите, если нужно что-то поправить 🙏"

# Первый ход после старта контейнера идёт дольше: прогреваются соединения,
# пусты все кэши. Отмечаем его в логе, чтобы не искать причину там, где её нет.
_cold_start = True

_ORDER_FLOW_PROMPT_PATH = Path(__file__).parent.parent / "dialog" / "prompts" / "order_flow_prompt.md"
# Ссылку подставляем из кода, а не пишем в промпт руками: иначе она разъедется
# с той, что возвращают инструменты, и бот начнёт слать две разные.
_ORDER_FLOW_PROMPT = _ORDER_FLOW_PROMPT_PATH.read_text(encoding="utf-8").replace(
    "{map_url}", CDEK_OFFICES_MAP_URL
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
                }
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
            "Не вызывай инструмент повторно, если способ доставки не менялся: "
            "чтобы прислать карту пунктов или просто ответить на вопрос, "
            "инструмент не нужен — ответь словами."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["cdek_pvz", "cdek_courier", "ozon_pvz", "russian_post"],
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
                        "Адрес пункта выдачи, который назвал клиент — для "
                        "cdek_pvz и ozon_pvz. Поле необязательное: без него "
                        "цена всё равно посчитается, а адрес спросишь "
                        "следующим сообщением. Для Ozon инструмент заодно "
                        "вернёт список пунктов города, чтобы клиент выбрал."
                    ),
                },
            },
            "required": ["method"],
        },
    },
    {
        "name": "set_recipient",
        "description": (
            "Записать получателя заказа. Нужен для доставки СДЭКом: без ФИО "
            "и телефона заказ в СДЭКе не завести. Спрашивай их после того, "
            "как клиент выбрал доставку."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "ФИО получателя"},
                "phone": {"type": "string", "description": "Телефон получателя"},
            },
            "required": ["name", "phone"],
        },
    },
    {
        "name": "confirm_order",
        "description": (
            "Зафиксировать согласие клиента оформить заказ, когда есть "
            "черновик, ожидающий подтверждения, и клиент явно согласился."
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
            },
            "required": ["question", "reason"],
        },
    },
]


@dataclass
class ToolExecution:
    # tool_result идёт обратно в Claude, чтобы модель сформулировала ответ.
    # client_reply — готовый ответ клиенту напрямую, без второго вызова
    # модели; заполняется только теми тулами, где текст полностью
    # детерминирован и не зависит от остального контекста реплики.
    tool_result: str
    client_reply: str | None = None


def _tools_for_stage(stage: str | None) -> list[dict]:
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
        stage_tools = [by_name["set_delivery_method"], by_name["set_recipient"]]
    elif stage == "awaiting_confirmation":
        stage_tools = [
            by_name["set_delivery_method"],
            by_name["set_recipient"],
            by_name["confirm_order"],
        ]
    else:
        stage_tools = [by_name["propose_order"]]
    return stage_tools + [by_name["escalate_to_manager"]]


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


def _describe_draft(draft: OrderDraft | None) -> str:
    if draft is None:
        return "Активного черновика заказа у клиента нет."

    lines = [f"Черновик заказа на этапе «{draft.stage}»:"]
    for item in draft.items:
        lines.append(f"- {item['name']} x{item['quantity']} = {item['price'] * item['quantity']} руб.")
    lines.append(f"Сумма товаров: {draft.items_total} руб.")
    if draft.delivery_label:
        lines.append(f"Способ доставки: {draft.delivery_label}, стоимость {draft.delivery_cost} руб.")

    # Показываем, что записано на самом деле. Без этого модель судит по
    # собственной прошлой реплике: написала клиенту «получатель записан», а
    # инструмент не вызвала — и узнаёт об этом только при подтверждении.
    name = draft.details.get("recipient_name")
    phone = draft.details.get("recipient_phone")
    if name and phone:
        lines.append(f"Получатель записан: {name}, {phone}.")
    elif draft.delivery_method in ("cdek_pvz", "cdek_courier"):
        lines.append(
            "Получатель ещё НЕ записан. Если клиент уже называл ФИО и телефон — "
            "вызови set_recipient с ними, не переспрашивая."
        )

    if draft.delivery_method == "cdek_pvz" and not draft.details.get("delivery_point"):
        lines.append("Пункт выдачи ещё НЕ выбран.")

    if draft.delivery_method == "ozon_pvz" and not draft.details.get("ozon_point_id"):
        lines.append("Пункт выдачи Ozon ещё НЕ выбран.")

    return "\n".join(lines)


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


async def _execute_propose_order(peer_id: int, tool_input: dict) -> str:
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
    await state.set_draft(peer_id, draft)

    lines = [f"{i['name']} x{i['quantity']} = {i['price'] * i['quantity']} руб." for i in resolved]
    result = "Черновик заказа создан:\n" + "\n".join(lines) + f"\nСумма товаров: {items_total} руб."
    if unresolved:
        result += f"\nНе нашли в ассортименте: {', '.join(unresolved)} — уточни у клиента точное название."
    # Пункт выдачи называем первым и объясняем почему: клиенту проще
    # согласиться на вариант, который уже предложен, чем выбирать из списка.
    # Первым идёт Ozon: на живом расчёте он вышел 121 руб против 397 у
    # СДЭКа. Платит за доставку клиент, так что порядок — это про то, какую
    # цену он увидит первой, а не про нашу выгоду.
    result += (
        "\nТеперь предложи клиенту доставку. Первым предлагай пункт выдачи "
        "Ozon — он заметно дешевле, клиент забирает посылку сам. Если нужно "
        "быстрее, предложи пункт выдачи СДЭК: дороже, но идёт в полтора-два "
        "раза меньше. Курьер СДЭК до двери и Почта России — тоже можно. "
        "Для любого расчёта спроси город клиента: без него стоимость не "
        "посчитать. У Ozon цену можно назвать только после того, как клиент "
        "выберет пункт: вызови set_delivery_method с городом, инструмент "
        "вернёт список пунктов. Для пункта выдачи СДЭК нужен адрес самого "
        "пункта — предложи карту, если клиент не знает ближайший: "
        f"{CDEK_OFFICES_MAP_URL}"
    )
    return result


def _draft_weight_grams(draft: OrderDraft) -> int:
    quantity = sum(item.get("quantity", 1) for item in draft.items) or 1
    return settings.cdek_default_package_weight_grams * quantity


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
) -> tuple[list, int]:
    """Пункты Ozon под то, что назвал клиент, и сколько их нашлось всего."""
    return await ozon_quote.points_for(
        city,
        hint,
        weight_grams=_draft_weight_grams(draft),
        declared_value=draft.items_total,
    )


async def _ozon_price(draft: OrderDraft, point_id: int) -> ozon_client.Quote:
    """Во что обойдётся доставка Ozon в конкретный пункт.

    Телефон нужен самому Ozon для расчёта. Берём телефон получателя, если он
    уже записан, иначе служебный: цену клиент должен увидеть раньше, чем мы
    попросим его данные, — иначе получается допрос до первой цифры.
    """
    return await ozon_quote.price_for(
        point_id,
        phone=draft.details.get("recipient_phone", ""),
        weight_grams=_draft_weight_grams(draft),
        declared_value=draft.items_total,
    )


async def _execute_set_delivery_method(peer_id: int, tool_input: dict) -> ToolExecution:
    draft = await state.get_draft(peer_id)
    # Способ доставки можно уточнять и после того, как цена названа: клиент
    # передумывает, а адрес пункта выдачи приходит отдельным сообщением.
    if draft is None or draft.stage not in ("awaiting_delivery", "awaiting_confirmation"):
        return ToolExecution(
            "Нет черновика заказа, ожидающего выбора доставки. Уточни у клиента, что он хочет заказать."
        )

    method = tool_input.get("method")
    period = ""
    ask_for_point = False
    ozon_options = ""

    if method in ("cdek_pvz", "cdek_courier"):
        address = (tool_input.get("address") or "").strip()
        if not address:
            return ToolExecution(
                "Чтобы посчитать доставку СДЭКом, нужен город клиента "
                "(для курьера — полный адрес). Спроси и вызови инструмент ещё раз."
            )
        try:
            tariff, total = await _cdek_delivery(draft, method, address)
        except Exception:
            logger.exception("Не посчитали доставку СДЭК для peer_id=%s по адресу «%s»", peer_id, address)
            return ToolExecution(
                "Расчёт СДЭКа сейчас недоступен. Скажи клиенту, что стоимость "
                "доставки уточнит менеджер, и вызови escalate_to_manager."
            )

        period = tariff.period
        label = "СДЭК, курьером до адреса" if method == "cdek_courier" else "СДЭК, пункт выдачи"
        draft.details["tariff_code"] = tariff.code
        draft.details["address"] = address

        if method == "cdek_pvz":
            hint = (tool_input.get("pickup_point") or "").strip()
            if not hint:
                ask_for_point = True
                draft.details.pop("delivery_point", None)
            else:
                # СДЭКу нужен код пункта, а клиент называет адрес словами,
                # поэтому ищем совпадение по списку пунктов города.
                try:
                    found = await cdek_client.find_delivery_point(address, hint)
                except Exception:
                    logger.exception("Не нашли пункты выдачи в «%s» для peer_id=%s", address, peer_id)
                    found = []

                if len(found) == 1:
                    draft.details["delivery_point"] = found[0].code
                    label = f"{label}: {found[0].address}"
                    # С известным пунктом цена может отличаться, поэтому
                    # пересчитываем: платит клиент ровно то, что выставят нам.
                    try:
                        tariff, total = await _cdek_delivery(
                            draft, method, address, delivery_point=found[0].code
                        )
                    except Exception:
                        logger.exception(
                            "Не пересчитали доставку с пунктом %s для peer_id=%s",
                            found[0].code, peer_id,
                        )
                elif found:
                    options = "; ".join(f"{i}) {p.describe()}" for i, p in enumerate(found, start=1))
                    await state.set_draft(peer_id, draft)
                    return ToolExecution(
                        f"По запросу «{hint}» в городе {address} нашлось несколько пунктов: "
                        f"{options}. Перечисли их клиенту и спроси, какой из них, а потом "
                        "вызови set_delivery_method ещё раз с точным адресом в pickup_point."
                    )
                else:
                    await state.set_draft(peer_id, draft)
                    return ToolExecution(
                        f"Пункт выдачи «{hint}» в городе {address} не нашёлся. Попроси "
                        "клиента уточнить адрес и предложи карту пунктов: "
                        f"{CDEK_OFFICES_MAP_URL}"
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
        try:
            points, found = await _ozon_points(draft, city, hint)
        except Exception:
            logger.exception("Не подобрали пункт Ozon в «%s» для peer_id=%s", city, peer_id)
            points, found = [], 0

        if not points:
            if found:
                return ToolExecution(
                    f"Пункты Ozon в городе {city} есть, но доставку нашим методом "
                    "они не принимают. Предложи клиенту пункт выдачи СДЭК."
                )
            asked = f"«{hint}» " if hint else ""
            return ToolExecution(
                f"Пункт выдачи Ozon {asked}в городе {city} не нашёлся. Уточни у "
                "клиента адрес пункта или предложи доставку СДЭКом."
            )

        # Цену считаем по первому подходящему пункту и называем сразу, даже
        # когда клиент ещё не выбрал. На живой проверке она от пункта не
        # зависела: Владивосток, два пункта на разных концах города — 176 руб
        # оба. Ждать выбора значит растягивать разговор на лишний круг ради
        # цифры, которая, скорее всего, не изменится. А если где-то всё-таки
        # изменится — второй вызов с выбранным пунктом пересчитает, и до
        # подтверждения клиент услышит верную сумму.
        point = points[0]
        try:
            quote = await _ozon_price(draft, point.id)
        except Exception:
            logger.exception(
                "Не посчитали доставку Ozon в пункт %s для peer_id=%s", point.id, peer_id
            )
            return ToolExecution(
                "Расчёт Ozon сейчас недоступен. Предложи клиенту доставку СДЭКом, "
                "а если он хочет именно Ozon — вызови escalate_to_manager."
            )

        draft.details["address"] = city
        # Тот же урок, что и с СДЭКом: страховку Ozon выставляет отдельной
        # строкой, и «забыть» её значит доплачивать за клиента.
        draft.delivery_cost = quote.total
        period = f"{quote.days} дн." if quote.days else ""

        if len(points) > 1:
            # Пункт не фиксируем: показанная цена относится к первому из
            # списка, а поедет посылка туда, что выберет клиент.
            draft.details.pop("ozon_point_id", None)
            draft.details.pop("ozon_point_address", None)
            draft.delivery_label = "Ozon, пункт выдачи"
            ozon_options = "; ".join(f"{i}) {p.address}" for i, p in enumerate(points, start=1))
        else:
            draft.details["ozon_point_id"] = point.id
            draft.details["ozon_point_address"] = point.address
            draft.delivery_label = f"Ozon, пункт выдачи: {point.address}"
    else:
        tariffs = _load_tariffs()
        tariff = tariffs.get(method)
        if tariff is None:
            return ToolExecution(
                f"Неизвестный способ доставки: {method}. Предложи клиенту выбрать из вариантов ещё раз."
            )
        draft.delivery_label = tariff["label"]
        draft.delivery_cost = tariff["price"]

    draft.delivery_method = method
    draft.stage = "awaiting_confirmation"
    await state.set_draft(peer_id, draft)

    total = draft.items_total + draft.delivery_cost
    # Срок у СДЭКа уже заканчивается точкой («3–4 раб. дн.»), своей не добавляем.
    head = f"Способ доставки зафиксирован: {draft.delivery_label}, {draft.delivery_cost} руб"
    head += f", срок {period}\n" if period else ".\n"
    head += f"Сумма товаров: {draft.items_total} руб. Итого с доставкой: {total} руб.\n"

    # Формулировку отдаём модели: с очередью второй заход к Claude перестал
    # быть роскошью, а живой текст клиенту приятнее нашего шаблона. Пока
    # висел восьмисекундный потолок VK, этот заход приходилось вырезать.
    if ask_for_point:
        return ToolExecution(
            head
            + "Сообщи клиенту эти суммы и спроси, в какой пункт выдачи СДЭК ему "
            "удобно забрать заказ — нужен адрес пункта, а не просто город. "
            f"Предложи прислать карту пунктов, чтобы свериться: {CDEK_OFFICES_MAP_URL}. "
            "Когда клиент назовёт адрес, вызови set_delivery_method ещё раз с тем "
            "же городом и адресом пункта в pickup_point."
        )

    if ozon_options:
        return ToolExecution(
            head
            + "Сообщи клиенту эти суммы и перечисли пункты выдачи Ozon: "
            f"{ozon_options}. Спроси, какой ему удобнее, и вызови "
            "set_delivery_method ещё раз с тем же городом и адресом выбранного "
            "пункта в pickup_point."
        )

    next_step = (
        "Потом спроси ФИО получателя и телефон — они нужны для доставки СДЭКом."
        if method in ("cdek_pvz", "cdek_courier")
        else "Спроси, готов ли он оформить заказ."
    )
    return ToolExecution(head + "Сообщи клиенту эти суммы. " + next_step)


async def _execute_set_recipient(peer_id: int, tool_input: dict) -> str:
    draft = await state.get_draft(peer_id)
    if draft is None:
        return "Нет черновика заказа. Уточни у клиента, что он хочет заказать."

    name = (tool_input.get("name") or "").strip()
    phone = (tool_input.get("phone") or "").strip()
    if not name or not phone:
        return "Нужны и ФИО получателя, и телефон. Спроси у клиента то, чего не хватает."

    draft.details["recipient_name"] = name
    draft.details["recipient_phone"] = phone
    await state.set_draft(peer_id, draft)
    return (
        f"Получатель записан: {name}, {phone}. Если клиент уже согласился "
        "оформить заказ, вызывай confirm_order."
    )


async def _register_in_cdek(peer_id: int, draft: OrderDraft) -> str | None:
    """Завести заказ в СДЭКе. None — если не вышло: заказ доведёт менеджер."""
    number = f"vk{peer_id}-{int(time.time())}"
    try:
        registered = await cdek_client.register_order(
            number=number,
            tariff_code=draft.details["tariff_code"],
            recipient_name=draft.details["recipient_name"],
            recipient_phone=draft.details["recipient_phone"],
            items=draft.items,
            weight_grams=_draft_weight_grams(draft),
            to_address=draft.details.get("address"),
            delivery_point=draft.details.get("delivery_point"),
            comment=f"Заказ из ВК, диалог {vk_client.dialog_link(peer_id)}",
        )
    except Exception:
        logger.exception("Не завели заказ в СДЭКе для peer_id=%s", peer_id)
        await _notify_manager(
            peer_id,
            f"⚠️ Заказ подтверждён, но в СДЭК не уехал — завести руками.\n"
            f"Диалог: {vk_client.dialog_link(peer_id)}",
            # Всё, что про заказы, идёт в свой чат; пусто — значит менеджеру.
            chat_id=settings.telegram_orders_chat_id or None,
        )
        return None

    logger.info("Заказ %s заведён в СДЭКе: uuid=%s", number, registered.uuid)
    return registered.uuid


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
    if is_cdek and not (draft.details.get("recipient_name") and draft.details.get("recipient_phone")):
        return ToolExecution(
            "Для оформления доставки СДЭКом нужны ФИО получателя и телефон. "
            "Если клиент уже называл их в переписке — вызови set_recipient с "
            "этими данными прямо сейчас, не переспрашивая, и потом confirm_order. "
            "Если не называл — спроси."
        )

    draft.stage = "confirmed"
    cdek_uuid = await _register_in_cdek(peer_id, draft) if is_cdek else None

    try:
        await orders_repository.save_order(peer_id, draft, cdek_uuid)
    except Exception:
        logger.exception("Failed to persist order to database for peer_id=%s", peer_id)

    payment_message = await payment_service.generate_payment_link(draft)
    await state.clear_draft(peer_id)

    # Текст подтверждения полностью определён здесь и клиенту его можно
    # отдать как есть. Это снимает второй заход к Claude на самом дорогом
    # ходу диалога — те самые пара секунд, которых не хватало до таймаута VK.
    if is_cdek and cdek_uuid is None:
        reply = (
            f"Заказ подтверждён, но в СДЭКе его завести не удалось — этим займётся "
            f"менеджер. {payment_message}"
        )
    else:
        reply = f"Заказ подтверждён. {payment_message}"
    return ToolExecution(reply, client_reply=reply)


async def _notify_manager(peer_id: int, message: str, chat_id: str | None = None) -> None:
    try:
        await telegram_client.send_message(message, chat_id=chat_id)
    except Exception:
        logger.exception("Failed to notify manager via Telegram for peer_id=%s", peer_id)


async def _execute_escalate_to_manager(peer_id: int, tool_input: dict) -> ToolExecution:
    if await escalation_state.is_open(peer_id):
        return ToolExecution(
            tool_result=(
                "Вопрос этого клиента уже передан менеджеру и ждёт ответа — "
                "уведомлять менеджера второй раз не нужно. Коротко подтверди "
                "клиенту, что менеджер подключится, и не повторяй это в "
                "следующих ответах, если он сам не спросит."
            ),
            client_reply="Менеджер уже знает про этот вопрос и подключится, как только освободится 🙏",
        )

    question_raw = tool_input.get("question", "")
    reason_raw = tool_input.get("reason", "")
    question = html.escape(question_raw)
    reason = html.escape(reason_raw)
    dialog_link = vk_client.dialog_link(peer_id)
    message = f"<b>Вопрос клиента</b>\n{question}\n\n<b>Почему эскалировано</b>\n{reason}\n\n{dialog_link}"

    # Сначала фиксируем эскалацию у себя — это быстро и надёжно, и именно
    # эта запись, а не уведомление, остаётся следом того, что вопрос передан.
    await escalation_state.mark_open(peer_id)

    try:
        await escalation_log.record_escalation(peer_id, question_raw, reason_raw)
    except Exception:
        logger.exception("Failed to record escalation in database for peer_id=%s", peer_id)

    # Уведомление менеджеру ждём здесь же, но недолго: таймаут у клиента
    # Telegram теперь 2 секунды, а не 10, и весь бюджет VK он больше съесть
    # не может.
    #
    # Отправляли это в фон — не сработало: на serverless инстанс засыпает
    # сразу после ответа, и задача умирала, не дойдя до сети. В логах не
    # оставалось ни успеха, ни ошибки. Ограниченный по времени вызов в общем
    # пути хуже по задержке, но он хотя бы случается и оставляет след.
    await _notify_manager(peer_id, message)

    return ToolExecution(
        tool_result=(
            "Вопрос зафиксирован и передан менеджеру. Скажи клиенту, что уточнишь "
            "и вернёшься с ответом — не упоминай менеджера как адресата для "
            "обращения самого клиента, только что ты сам уточнишь и вернёшься."
        ),
        client_reply="Уточню это у менеджера и вернусь с ответом 🙏",
    )


async def _execute_tool(peer_id: int, name: str, tool_input: dict) -> ToolExecution:
    if name == "propose_order":
        return ToolExecution(await _execute_propose_order(peer_id, tool_input))
    if name == "set_delivery_method":
        return await _execute_set_delivery_method(peer_id, tool_input)
    if name == "confirm_order":
        return await _execute_confirm_order(peer_id)
    if name == "set_recipient":
        return ToolExecution(await _execute_set_recipient(peer_id, tool_input))
    if name == "escalate_to_manager":
        return await _execute_escalate_to_manager(peer_id, tool_input)
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


async def handle_turn(peer_id: int, user_text: str) -> str:
    global _cold_start
    started = time.monotonic()
    spent = _Spent()
    cold = _cold_start
    _cold_start = False
    try:
        return await _handle_turn(peer_id, user_text, spent)
    finally:
        logger.info(
            "ход peer_id=%s %s%s",
            peer_id,
            spent.describe(time.monotonic() - started),
            " (холодный старт)" if cold else "",
        )


async def _handle_turn(peer_id: int, user_text: str, spent: _Spent) -> str:
    catalog_context = await catalog_service.build_catalog_context()
    draft = await state.get_draft(peer_id)

    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"
    system_prompt += f"\n\n{_ORDER_FLOW_PROMPT}"
    system_prompt += f"\n\n{_describe_draft(draft)}"

    escalation_note = await _describe_escalation(peer_id)
    if escalation_note:
        system_prompt += f"\n\n{_ESCALATION_FLOW_PROMPT}\n\n{escalation_note}"

    tools = _tools_for_stage(draft.stage if draft else None)

    history = await dialog_history.get_history(peer_id)
    messages: list[dict] = history + [{"role": "user", "content": user_text}]

    response = await spent.claude(claude_client.converse(messages, system_prompt, tools))

    if response.stop_reason != "tool_use":
        reply = claude_client.extract_text(response)
        await dialog_history.append_exchange(peer_id, user_text, reply)
        return reply

    tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
    messages.append({"role": "assistant", "content": response.content})

    executions = [
        (block, await spent.tool(_execute_tool(peer_id, block.name, block.input)))
        for block in tool_use_blocks
    ]

    # Если за ход выполнился ровно один инструмент и ответ клиенту у него
    # детерминированный (сейчас так только у escalate_to_manager), отдаём этот
    # текст напрямую. Второй запрос к Claude нужен лишь чтобы пересказать то же
    # самое своими словами, а стоит он несколько секунд — из-за него путь
    # эскалации не укладывался в таймаут вебхука VK: тот рвал соединение
    # (в логах ERROR Code 499), ответ клиенту отправить уже не успевали, и
    # человек не получал ничего.
    #
    # Раньше здесь дополнительно требовалось, чтобы модель не написала своего
    # текста рядом с вызовом инструмента, — и на первой эскалации это условие
    # обычно не выполнялось, так что короткий путь не срабатывал именно там,
    # где был нужнее всего. Текст модели при этом теряется: осознанный размен,
    # предсказуемый ответ за две секунды полезнее красивого, который не дошёл.
    # Условие «ровно один инструмент» было слишком узким. Когда за ход
    # срабатывали set_recipient и confirm_order, готовый текст уходил не
    # клиенту, а в Claude на пересказ — и модель теряла из него важное.
    # Так клиент прочитал «Заказ оформлен! ✅» вместо честного «заказ
    # подтверждён, но в СДЭК не уехал». Смотрим на последний инструмент:
    # он и есть итог хода.
    if executions and executions[-1][1].client_reply is not None:
        reply = executions[-1][1].client_reply
        await dialog_history.append_exchange(peer_id, user_text, reply)
        return reply

    tool_results = [
        {"type": "tool_result", "tool_use_id": block.id, "content": execution.tool_result}
        for block, execution in executions
    ]
    messages.append({"role": "user", "content": tool_results})

    follow_up = await spent.claude(claude_client.converse(messages, system_prompt, tools))

    # Модель может попросить ещё один инструмент вместо того, чтобы ответить
    # словами. Раньше здесь падало с «no text block», и клиент получал
    # «Извините, сейчас не получается ответить» — то есть ошибка кода
    # выглядела как поломка бота.
    #
    # Третий заход к Claude не делаем: два обращения уже съедают большую часть
    # из восьми секунд VK. Инструмент выполняем — терять действие нельзя, —
    # а отвечаем тем, что он вернул клиенту напрямую.
    if follow_up.stop_reason == "tool_use":
        extra = [block for block in follow_up.content if block.type == "tool_use"]
        logger.warning(
            "Модель запросила инструменты второй раз подряд для peer_id=%s: %s",
            peer_id,
            [block.name for block in extra],
        )
        direct_reply = None
        for block in extra:
            execution = await spent.tool(_execute_tool(peer_id, block.name, block.input))
            if direct_reply is None and execution.client_reply is not None:
                direct_reply = execution.client_reply

        reply = direct_reply or claude_client.extract_text(follow_up, default=_SECOND_ROUND_FALLBACK)
        await dialog_history.append_exchange(peer_id, user_text, reply)
        return reply

    reply = claude_client.extract_text(follow_up)
    await dialog_history.append_exchange(peer_id, user_text, reply)
    return reply
