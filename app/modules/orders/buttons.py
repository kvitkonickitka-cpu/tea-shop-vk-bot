"""Кнопки под сообщениями бота: какие поставить и что делать по нажатию.

Нажатие обрабатывает код, без модели: «2» на кнопке пункта — это ровно
второй пункт из показанного списка, а «Оформить» — счёт тем же путём, что
и в диалоге. Модели достаётся только то, что код решить не может: «Нет»,
«Изменить», «Выбрать другое» и старая кнопка.

Payload — короткий JSON: `a` — действие, `o` — номер заказа, `v` — версия
черновика, `n` — номер пункта. Клиент может подменить его руками, поэтому
перед действием проверяем, что заказ его, этап подходящий и версия та же.
Не сходится — «Эта кнопка уже неактуальна», и текст нажатия идёт модели.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.messages import funnel, keyboard as keyboards, templates
from app.core.config import settings
from app.modules.orders import geo
from app.modules.orders import eta, points, repository as orders_repository, state, take

logger = logging.getLogger(__name__)

# Клавиатура, которую код приготовил за ход (счёт → «Оплатить»). Ход в
# диалоге один (блокировка), и отправка идёт в том же процессе сразу после
# хода, так что словаря по peer_id хватает.
_stashed: dict[int, dict] = {}
# Готовая к отправке клавиатура (JSON) — её забирает отправка ответа.
_ready: dict[int, str] = {}


def stash(peer_id: int, keyboard: dict | None) -> None:
    if keyboard is not None:
        _stashed[peer_id] = keyboard


def take_ready(peer_id: int) -> str | None:
    return _ready.pop(peer_id, None)


def pay_keyboard(total, link: str) -> dict | None:
    return keyboards.inline([[keyboards.link_button(f"Оплатить {templates.amount(total)} ₽", link)]])


async def for_reply(peer_id: int, *, with_points: bool = True, reply: str = "") -> tuple[dict | None, str]:
    """Клавиатура под ответом хода и подсказка к ней — по состоянию черновика.

    `with_points=False` — ход списка пунктов не показывал (модель спросила
    «пункт или курьер?», ответила на «не пишите мне»): кнопки пунктов под
    таким ответом не к месту, а под вопросом про СДЭК — ещё и чужие (Ozon).
    Исключение — ответ сам называет пункты из списка (`reply`).
    """
    kept = _stashed.pop(peer_id, None)
    if kept is not None:
        return kept, ""
    draft = await state.get_draft(peer_id)
    if draft is None:
        return None, ""
    details = draft.details
    fixed = details.get("ozon_point_id") or details.get("delivery_point")
    shown = details.get("shown_points") or []
    version = details.get("version")

    if details.get("offer"):
        return None, ""

    # Прошлый получатель — кнопкой: модель предлагает его текстом, как только
    # выбрана доставка, а получатель ещё не записан.
    recipient_row = []
    if (
        draft.delivery_method in ("ozon_pvz", "cdek_pvz", "cdek_courier")
        and not details.get("recipient_name")
        and not details.get("email_suggestion")
        # Получатель из формы «Товаров» — другой человек: прошлого не предлагаем.
        and not details.get("storefront_recipient")
    ):
        from app.modules.orders import repeat_delivery

        last = await repeat_delivery.last_recipient_for(peer_id)
        if last is not None and last.email:
            recipient_row = [keyboards.text_button(
                "Да, на эти данные", {"a": "last_recipient", "v": version}, "positive"
            )]

    # Кнопка геопозиции — там, где бот спрашивает, где забрать: до выбора
    # доставки и в большом городе, пока пункт не назван.
    geo_row = []
    wants_geo = (not draft.delivery_method and draft.stage == "awaiting_delivery") or (
        details.get("point_asked") and not shown and not fixed
        and draft.delivery_method in ("ozon_pvz", "cdek_pvz")
    )
    if wants_geo and await geo.offer_for(peer_id, draft.delivery_method):
        geo_row = [geo.button(version)]

    if shown and not fixed and draft.delivery_method and (with_points or points.mentioned(reply, shown)):
        rows = [
            [keyboards.text_button(
                f"{point['n']}. {points.short(point['address'], 34)}",
                {"a": "pt", "n": point["n"], "v": version},
            )]
            for point in shown
        ]
        return keyboards.inline(rows + [recipient_row]), templates.POINTS_HINT

    if recipient_row:
        return keyboards.inline([geo_row, recipient_row]), ""

    if details.get("email_suggestion") and details.get("pending_recipient"):
        return keyboards.inline([[
            # Подпись — только домен: целиком адрес ВК обрезает («Да, kvitko…@gma…»).
            keyboards.text_button(f"Да, @{details['email_suggestion'].rpartition('@')[2]}",
                                  {"a": "email_yes", "v": version}, "positive"),
            keyboards.text_button("Нет", {"a": "email_no", "v": version}),
        ]]), templates.EMAIL_HINT

    item = details.get("upsell_item")
    if (
        item and not details.get("upsell_button_sent")
        and draft.stage == "awaiting_delivery"
        and item not in {row["name"] for row in draft.items}
    ):
        # Кнопка допродажи — один раз, под тем же сообщением, что и
        # предложение. Отметка меняет версию, поэтому версию берём после.
        details["upsell_button_sent"] = True
        await state.set_draft(peer_id, draft)
        return keyboards.inline([[keyboards.text_button(
            f"Добавить {item}", {"a": "add", "v": draft.details.get("version")}, "positive"
        )], geo_row]), ""
    if geo_row:
        return keyboards.inline([geo_row]), ""
    return None, ""


async def _consult_keyboard(peer_id: int, reply: str) -> tuple[dict | None, list[str] | None]:
    """«Взять <сорт>» под консультацией или «Добавить <сорт>» на этапе доставки."""
    from app.modules.catalog import service as catalog_service

    if not settings.take_buttons_enabled or take.unsuitable(reply):
        return None, None
    draft = await state.get_draft(peer_id)
    if draft is None:
        # После выставления счёта под консультацией кнопок нет.
        if await orders_repository.live_invoice_order(peer_id) is not None:
            return None, None
        found = take.mentioned(reply, catalog_service.load_items())
        prefix, action, version = "Взять", "take", None
    elif draft.stage == "awaiting_delivery":
        found = take.mentioned(
            reply, catalog_service.load_items(), exclude={row["name"] for row in draft.items}
        )
        prefix, action, version = "Добавить", "add_item", draft.details.get("version")
    else:
        return None, None
    if not found:
        return None, None
    names = [item["name"] for item in found]
    if await take.last_set(peer_id) == names:
        # Тот же набор уже под предыдущим сообщением — не повторяем.
        return None, None
    rows = []
    for item in found:
        payload = {"a": action, "n": item["name"]}
        if version is not None:
            payload["v"] = version
        rows.append([keyboards.text_button(take.label(prefix, item), payload, "positive")])
    return keyboards.inline(rows), names


async def prepare(peer_id: int, reply: str, *, consult: bool = False, with_points: bool = True) -> str:
    """Решить, пойдёт ли под ответом клавиатура; вернуть ответ с подсказкой.

    Подсказку дописываем, только когда кнопки клиент действительно увидит,
    и до записи в историю — туда попадает ровно то, что ушло клиенту.
    `consult` — ответ модели словами, не шаблон и не эскалация: под ним
    можно поставить «Взять».
    """
    names = None
    try:
        keyboard, hint = await for_reply(peer_id, with_points=with_points, reply=reply)
        if keyboard is None and consult:
            keyboard, names = await _consult_keyboard(peer_id, reply)
        markup = await keyboards.for_peer(peer_id, keyboard)
    except Exception:
        logger.exception("Не собрали кнопки для peer_id=%s", peer_id)
        return reply
    await take.remember_set(peer_id, names if markup is not None else None)
    if markup is not None:
        await geo.note_shown(peer_id, keyboard, "dialog")
    if markup is not None and names:
        await funnel.record(peer_id, "take_shown", source_=funnel.CODE, items=names)
    if markup is None:
        return reply
    _ready[peer_id] = markup
    return f"{reply}\n{hint}" if hint and hint not in reply else reply


@dataclass
class Press:
    """Итог нажатия: что ответить и передавать ли текст нажатия модели."""

    reply: str | None = None
    keyboard: dict | None = None
    to_model: bool = False


# Старая кнопка — модели не отдаём. Подпись «Да, …@gmail.com» после правки
# заказа модель прочла как согласие на последнюю правку и повторила её:
# в заказе стало три пачки вместо двух (05.10.2026). Код отвечает сам —
# «неактуальна» и что нужно дальше по текущему черновику.
STALE = Press(reply=templates.button_stale())
# Старая «Взять <сорт>» / «Добавить <сорт>» — намерение ясно из подписи:
# такую модели отдать можно, она добавит сорт или скажет, что он уже есть.
STALE_TO_MODEL = Press(reply=templates.button_stale(), to_model=True)
TO_MODEL = Press(to_model=True)


async def handle(peer_id: int, message: dict) -> Press:
    payload = keyboards.parse_payload(message.get("payload"))
    if payload is None:
        return TO_MODEL
    action = payload["a"]
    handler = _HANDLERS.get(action)
    if handler is None:
        return TO_MODEL
    order_id = payload.get("o") if isinstance(payload.get("o"), int) else None
    await funnel.record(
        peer_id, f"button:{action}", order_id=order_id, source_=funnel.BUTTON, version=payload.get("v"),
        touch=payload.get("t") if isinstance(payload.get("t"), str) else None,
    )
    # Под ответом на нажатие свои кнопки — набор «Взять» под прошлым
    # сообщением больше не последний.
    await take.remember_set(peer_id, None)
    try:
        # Всё, что случится внутри нажатия (черновик, пункт, счёт), — с кнопки.
        with funnel.source(funnel.BUTTON):
            press = await handler(peer_id, payload)
    except Exception:
        logger.exception("Нажатие %s у peer_id=%s не обработалось, отдаём модели", action, peer_id)
        return TO_MODEL
    if press is STALE or press is STALE_TO_MODEL:
        await funnel.record(peer_id, "button_stale", order_id=order_id, source_=funnel.BUTTON, action=action)
    if press is STALE:
        return await _stale_reply(peer_id)
    return press


async def _stale_reply(peer_id: int) -> Press:
    """«Неактуальна» — и что нужно дальше по заказу, с теми кнопками, что актуальны."""
    try:
        draft = await state.get_draft(peer_id)
        if draft is not None:
            keyboard, _ = await for_reply(peer_id)
            return Press(reply=templates.button_stale(_missing(draft)), keyboard=keyboard)
        if await orders_repository.live_invoice_order(peer_id) is not None:
            return Press(reply=templates.button_stale(templates.STALE_LIVE_INVOICE))
    except Exception:
        logger.exception("Не собрали ответ на старую кнопку для peer_id=%s", peer_id)
    return Press(reply=templates.button_stale())


async def _draft_at(peer_id: int, payload: dict):
    """Черновик той же версии, при которой кнопку отправили, — или None."""
    draft = await state.get_draft(peer_id)
    if draft is None or draft.details.get("version") != payload.get("v"):
        return None
    return draft


async def _invoice_or(peer_id: int, fallback) -> Press:
    """Выставить счёт, если заказ полный, иначе — ответ `fallback()`."""
    from app.modules.orders import conversation

    invoiced = await conversation._auto_invoice(peer_id)
    if invoiced is not None and invoiced.client_reply is not None:
        return Press(reply=invoiced.client_reply, keyboard=_stashed.pop(peer_id, None))
    if invoiced is not None:
        # Итог изменился или товара нет — это разговор, его ведёт модель.
        return Press(reply=None, to_model=True)
    return await fallback()


def _missing(draft) -> str:
    details = draft.details
    if not draft.delivery_method:
        return templates.ASK_WHERE
    has_recipient = details.get("recipient_name") and details.get("recipient_email")
    if not (details.get("ozon_point_id") or details.get("delivery_point")) and draft.delivery_method != "cdek_courier":
        return templates.ASK_POINT if has_recipient else templates.ASK_POINT_AND_RECIPIENT
    return "" if has_recipient else templates.ASK_RECIPIENT


async def _on_point(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    if draft is None or not draft.delivery_method:
        return STALE
    shown = draft.details.get("shown_points") or []
    chosen = next((p for p in shown if p["n"] == payload.get("n")), None)
    if chosen is None:
        return STALE
    result = await conversation._execute_set_delivery_method(peer_id, {
        "method": draft.delivery_method,
        "address": draft.details.get("address", ""),
        "pickup_point": str(chosen["n"]),
    })
    fresh = await state.get_draft(peer_id)
    if fresh is None or not (fresh.details.get("ozon_point_id") or fresh.details.get("delivery_point")):
        # Перевозчик не посчитал этот пункт — объяснит модель.
        logger.info("peer_id=%s: пункт %s кнопкой не выбрался: %s", peer_id, chosen["n"], result.tool_result[:120])
        return TO_MODEL

    async def chosen_reply() -> Press:
        # Пункт записан, получателя нет. Получатель из заказа витрины уже
        # показан клиенту — просим только почту; у постоянного клиента под
        # ответом снова [Да, на эти данные]: кнопка под списком пунктов
        # после выбора пункта устарела.
        ask, keyboard = "", None
        candidate = fresh.details.get("storefront_recipient")
        if candidate and not fresh.details.get("recipient_name"):
            ask = templates.storefront_ask_email_only(candidate["name"], candidate["phone"])
        else:
            keyboard, _ = await for_reply(peer_id)
            if keyboard is not None:
                from app.modules.orders import repeat_delivery

                last = await repeat_delivery.last_recipient_for(peer_id)
                if last is not None and last.email:
                    ask = templates.ask_last_recipient(last.name, last.phone, last.email)
        return Press(reply=templates.point_chosen(
            address=chosen["address"],
            delivery_cost=fresh.delivery_cost,
            total=fresh.items_total + (fresh.delivery_cost or 0),
            ask_recipient=True,
            eta=eta.phrase(fresh.details),
            ask=ask,
            surcharge=bool(fresh.details.get("delivery_surcharge")),
        ), keyboard=keyboard)

    return await _invoice_or(peer_id, chosen_reply)


def carrier_keyboard(version, options: list[dict]) -> dict | None:
    """Заказ из «Товаров»: по кнопке на перевозчика — с ценой до города."""
    return keyboards.inline([
        [keyboards.text_button(templates.carrier_button(option), {"a": "ship", "m": option["method"], "v": version})]
        for option in options
    ])


async def _on_ship(peer_id: int, payload: dict) -> Press:
    """Выбран перевозчик для заказа из «Товаров»: дальше пункт и почта для чека."""
    from app.modules.orders import service as orders_service

    draft = await _draft_at(peer_id, payload)
    if draft is None or payload.get("m") not in ("ozon_pvz", "cdek_pvz") or not draft.details.get("storefront_city"):
        return STALE
    shown = await orders_service.after_carrier(peer_id, payload["m"])
    if shown is None:
        # Перевозчик не посчитал или пунктов нет — объяснит модель.
        return TO_MODEL
    text, keyboard = shown
    return Press(reply=text, keyboard=keyboard)


async def _on_add(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    item = draft.details.get("upsell_item") if draft else None
    if draft is None or not item:
        return STALE
    if draft.details.get("offer"):
        await funnel.record(peer_id, "upsell_accepted", item=item)
        return await _add_to_offer(peer_id, item)
    had_delivery = draft.delivery_method
    city = draft.details.get("address", "")
    await conversation._execute_add_to_order(peer_id, {"items": [{"name": item, "quantity": 1}]})
    fresh = await state.get_draft(peer_id)
    if fresh is None or item not in {row["name"] for row in fresh.items}:
        return TO_MODEL
    if had_delivery and city:
        # Вес вырос — доставку пересчитываем по тому же пункту: add_to_order
        # оставил его в списке под номером 1.
        pickup = "1" if fresh.details.get("shown_points") else ""
        await conversation._execute_set_delivery_method(
            peer_id, {"method": had_delivery, "address": city, "pickup_point": pickup}
        )
        fresh = await state.get_draft(peer_id)

    async def added_reply() -> Press:
        gap = conversation._threshold_gap(fresh.items_total)
        free = gap is None and conversation.free_delivery_threshold() is not None
        return Press(reply=templates.item_added(
            name=item, items_total=fresh.items_total, gap=gap, free=free,
            next_step=_missing(fresh),
        ))

    return await _invoice_or(peer_id, added_reply)


async def _on_email_yes(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    if draft is None:
        return STALE
    suggestion = draft.details.get("email_suggestion")
    pending = draft.details.get("pending_recipient") or {}
    if not suggestion or not pending.get("name"):
        return STALE
    result = await conversation._execute_set_recipient(
        peer_id, {"name": pending["name"], "phone": pending.get("phone", ""), "email": suggestion}
    )
    fresh = await state.get_draft(peer_id)
    if fresh is None or fresh.details.get("recipient_email") != suggestion:
        logger.info("peer_id=%s: почта кнопкой не записалась: %s", peer_id, result[:120])
        return TO_MODEL

    async def written() -> Press:
        # Пункт ещё не выбран — кнопки пунктов снова под ответом.
        keyboard, _ = await for_reply(peer_id)
        return Press(reply=templates.email_written(suggestion, _missing(fresh)), keyboard=keyboard)

    return await _invoice_or(peer_id, written)


async def _on_email_no(peer_id: int, payload: dict) -> Press:
    draft = await _draft_at(peer_id, payload)
    if draft is None:
        return STALE
    return TO_MODEL


async def _on_new_link(peer_id: int, payload: dict) -> Press:
    order_id = payload.get("o")
    if not isinstance(order_id, int):
        return STALE
    order = await orders_repository.by_id(order_id)
    draft = await state.get_draft(peer_id)
    if (
        order is None or order.peer_id != peer_id
        or order.payment_status == orders_repository.PAID
        or order.status == orders_repository.CANCELED
        or draft is None or draft.details.get("order_id") != order_id
    ):
        return STALE

    async def not_ready() -> Press:
        return TO_MODEL

    return await _invoice_or(peer_id, not_ready)


async def _on_checkout(peer_id: int, payload: dict) -> Press:
    draft = await _draft_at(peer_id, payload)
    if draft is None or draft.details.get("order_id") and payload.get("o") not in (None, draft.details.get("order_id")):
        return STALE

    async def ask() -> Press:
        return Press(reply=_missing(draft))

    return await _invoice_or(peer_id, ask)


async def _add_to_offer(peer_id: int, item: str) -> Press:
    """«Добавить» под предложением «как в прошлый раз»: то же сообщение заново.

    Вес вырос, поэтому пункт и цену проверяем снова — тем же путём, что и
    при первом предложении.
    """
    from app.modules.orders import conversation, offers
    from app.modules.orders.repeat_delivery import LastDelivery, LastRecipient

    await conversation._execute_add_to_order(peer_id, {"items": [{"name": item, "quantity": 1}]})
    draft = await state.get_draft(peer_id)
    old = offers.Offer.from_details(draft.details.get("offer")) if draft else None
    if old is None or item not in {row["name"] for row in draft.items}:
        return TO_MODEL
    offer = await offers.prepare(
        draft,
        LastDelivery(0, old.method, old.city, old.point_address, old.point_id),
        LastRecipient(0, old.name, old.phone, old.email),
    )
    if not offer.ready:
        return TO_MODEL
    draft.details["offer"] = offer.to_details()
    draft.details.pop("upsell_item", None)
    await state.set_draft(peer_id, draft)
    draft = await state.get_draft(peer_id)
    return Press(
        reply=conversation._offer_message(draft, offer),
        keyboard=conversation._offer_keyboard(draft),
    )


async def _on_add_more(peer_id: int, payload: dict) -> Press:
    """«Добавить <сорт>» под сводкой со ссылкой: новая сумма — новая ссылка.

    Черновика уже нет — он убран при выставлении счёта. Возвращаем его из
    заказа тем же путём, что и правку после ссылки словами: номер тот же,
    доставка пересчитывается по тому же пункту (вес вырос), новый счёт
    закрывает прежнюю попытку.
    """
    from app.modules.catalog import service as catalog_service
    from app.modules.orders import conversation

    order_id, name = payload.get("o"), payload.get("n")
    if not isinstance(order_id, int) or not isinstance(name, str):
        return STALE
    live = await orders_repository.live_invoice_order(peer_id)
    if live is None or live.id != order_id:
        return STALE
    match = catalog_service.find_item(name)
    if match is None or not match.get("in_stock", True):
        return STALE
    draft = await conversation._draft_for_edit(peer_id)
    if draft is None or draft.details.get("order_id") != order_id:
        return STALE
    if match["name"] in {row["name"] for row in draft.items}:
        return STALE
    method, city = draft.delivery_method, draft.details.get("address", "")
    await conversation._execute_add_to_order(peer_id, {"items": [{"name": match["name"], "quantity": 1}]})
    fresh = await state.get_draft(peer_id)
    if fresh is None or match["name"] not in {row["name"] for row in fresh.items} or not (method and city):
        return TO_MODEL
    pickup = "1" if fresh.details.get("shown_points") else ""
    await conversation._execute_set_delivery_method(
        peer_id, {"method": method, "address": city, "pickup_point": pickup}
    )

    async def not_ready() -> Press:
        return TO_MODEL

    return await _invoice_or(peer_id, not_ready)


def _catalog_item(name) -> dict | None:
    """Товар каталога ровно с этим названием и в наличии — или None."""
    from app.modules.catalog import service as catalog_service

    if not isinstance(name, str):
        return None
    match = catalog_service.find_item(name)
    if match is None or match["name"] != name or not match.get("in_stock", True):
        return None
    return match


async def _on_take(peer_id: int, payload: dict) -> Press:
    """«Взять <сорт>» под консультацией: черновик на пачку кодом.

    Постоянному клиенту propose_order сам пришлёт заказ как в прошлый раз со
    ссылкой (если всё сошлось); остальным — «Записала» и вопрос, куда везти.
    """
    from app.modules.catalog import service as catalog_service
    from app.modules.orders import conversation

    match = _catalog_item(payload.get("n"))
    if match is None:
        return STALE
    if await state.get_draft(peer_id) is not None or await orders_repository.live_invoice_order(peer_id) is not None:
        return STALE_TO_MODEL
    token = conversation._draft_origin.set("take")
    try:
        result = await conversation._execute_propose_order(
            peer_id, {"items": [{"name": match["name"], "quantity": 1}]}
        )
    finally:
        conversation._draft_origin.reset(token)
    if isinstance(result, conversation.ToolExecution) and result.client_reply is not None:
        return Press(reply=result.client_reply, keyboard=_stashed.pop(peer_id, None))
    draft = await state.get_draft(peer_id)
    if draft is None:
        return TO_MODEL
    item = draft.details.get("upsell_item")
    upsell = catalog_service.find_item(item) if item else None
    rows = []
    if upsell is not None and upsell.get("in_stock", True) and item not in {r["name"] for r in draft.items}:
        draft.details["upsell_button_sent"] = True
        await state.set_draft(peer_id, draft)
        draft = await state.get_draft(peer_id)
        rows.append([keyboards.text_button(
            f"Добавить {item}", {"a": "add", "v": draft.details.get("version")}, "positive"
        )])
    else:
        upsell = None
    with_geo = await geo.offer_for(peer_id)
    if with_geo:
        rows.append([geo.button(draft.details.get("version"))])
    keyboard = keyboards.inline(rows) if rows else None
    if with_geo:
        await geo.note_shown(peer_id, keyboard, "take")
    return Press(reply=templates.taken(
        name=take.display_name(match["name"]), price=match["price"],
        upsell=item if upsell else "", upsell_price=upsell["price"] if upsell else None,
        gap=conversation._threshold_gap(draft.items_total) if upsell else None, geo=with_geo,
    ), keyboard=keyboard)


async def _on_add_item(peer_id: int, payload: dict) -> Press:
    """«Добавить <сорт>» под консультацией, пока доставка не выбрана."""
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    match = _catalog_item(payload.get("n"))
    if match is None or (draft is not None and match["name"] in {row["name"] for row in draft.items}):
        return STALE
    if draft is None or draft.stage != "awaiting_delivery":
        return STALE_TO_MODEL
    await conversation._execute_add_to_order(peer_id, {"items": [{"name": match["name"], "quantity": 1}]})
    fresh = await state.get_draft(peer_id)
    if fresh is None or match["name"] not in {row["name"] for row in fresh.items}:
        return TO_MODEL
    gap = conversation._threshold_gap(fresh.items_total)
    free = gap is None and conversation.free_delivery_threshold() is not None
    return Press(reply=templates.item_added(
        name=take.display_name(match["name"]), items_total=fresh.items_total, gap=gap, free=free,
        next_step=_missing(fresh),
    ))


async def _on_offer_ok(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    if draft is None or not draft.details.get("offer"):
        return STALE
    result = await conversation.accept_offer(peer_id)
    if result.client_reply is not None:
        return Press(reply=result.client_reply, keyboard=_stashed.pop(peer_id, None))
    return TO_MODEL


async def _on_repeat(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import repeat_order

    order_id = payload.get("o")
    if not isinstance(order_id, int):
        return STALE
    order = await orders_repository.by_id(order_id)
    if (
        order is None or order.peer_id != peer_id
        or order.payment_status != orders_repository.PAID
        or order.status in ("refunded", orders_repository.CANCELED)
        # Клиент уже собирает другой заказ или ждёт оплаты — повтор не к месту.
        or await state.get_draft(peer_id) is not None
        or await orders_repository.live_invoice_order(peer_id) is not None
    ):
        return STALE
    result = await repeat_order.repeat_order(peer_id, order_id)
    if result.client_reply is not None:
        return Press(reply=result.client_reply, keyboard=_stashed.pop(peer_id, None))
    # Не сошлось — черновик с пояснением уже лежит, дальше ведёт модель.
    return TO_MODEL


async def _on_last_recipient(peer_id: int, payload: dict) -> Press:
    """«Да, на эти данные» — прошлый получатель кодом, почта снова по DNS."""
    from app.modules.orders import conversation, repeat_delivery

    draft = await _draft_at(peer_id, payload)
    if draft is None or draft.details.get("recipient_name"):
        return STALE
    last = await repeat_delivery.last_recipient_for(peer_id)
    if last is None:
        return STALE
    result = await conversation._execute_set_recipient(
        peer_id, {"name": last.name, "phone": last.phone, "email": last.email}
    )
    fresh = await state.get_draft(peer_id)
    if fresh is None or not fresh.details.get("recipient_email"):
        # Почта больше не проходит проверку — разговор, его ведёт модель.
        logger.info("peer_id=%s: прошлый получатель кнопкой не записался: %s", peer_id, result[:120])
        return TO_MODEL

    async def written() -> Press:
        details = fresh.details
        # Пункт ещё не выбран — кнопки пунктов снова под ответом.
        keyboard, _ = await for_reply(peer_id)
        return Press(reply=templates.recipient_written(
            name=details["recipient_name"], phone=details["recipient_phone"],
            email=details["recipient_email"], next_step=_missing(fresh),
        ), keyboard=keyboard)

    return await _invoice_or(peer_id, written)


async def _on_rate(peer_id: int, payload: dict) -> Press:
    """Оценка кнопкой под «Как вам чай?». Перенажатие меняет оценку."""
    from app.modules.dialog import vk_client
    from app.modules.orders import feedback
    from app.messages import manager as manager_messages

    rating = payload.get("r")
    order = await orders_repository.by_id(payload.get("o")) if isinstance(payload.get("o"), int) else None
    if (
        rating not in templates.RATINGS
        or order is None
        or order.peer_id != peer_id
        or order.payment_status != orders_repository.PAID
        or order.delivered_at is None
    ):
        return STALE
    changed = await feedback.rate(peer_id, order.id, rating, "button")
    if changed and rating in ("great", "no"):
        # «Не моё» — не эскалация: бот продолжает разговор сам, менеджеру — для сведения.
        await manager_messages.notify(
            manager_messages.FEEDBACK,
            templates.manager_rating(order.id, rating, vk_client.dialog_link(peer_id)),
            order_id=order.id, peer_id=peer_id,
        )
    reply = {"great": templates.rated_great, "ok": templates.rated_ok, "no": templates.rated_no}[rating]()
    return Press(reply=reply)


async def _to_model(peer_id: int, payload: dict) -> Press:
    return TO_MODEL


_HANDLERS = {
    "pt": _on_point,
    "ship": _on_ship,
    "add": _on_add,
    "email_yes": _on_email_yes,
    "email_no": _on_email_no,
    "new_link": _on_new_link,
    "checkout": _on_checkout,
    "offer_ok": _on_offer_ok,
    "repeat": _on_repeat,
    "last_recipient": _on_last_recipient,
    "add_more": _on_add_more,
    "take": _on_take,
    "add_item": _on_add_item,
    "edit": _to_model,
    "rate": _on_rate,
    # «Подобрать чай» под реактивацией — консультация, её ведёт модель.
    "advise": _to_model,
    "other": _to_model,
}


def register(action: str, handler) -> None:
    """Добавить действие — для «Оформить» постоянного клиента и «Повторить»."""
    _HANDLERS[action] = handler
