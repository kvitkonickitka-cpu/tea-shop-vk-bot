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
from app.modules.orders import points, repository as orders_repository, state

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


async def for_reply(peer_id: int) -> tuple[dict | None, str]:
    """Клавиатура под ответом хода и подсказка к ней — по состоянию черновика."""
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

    if shown and not fixed and draft.delivery_method:
        rows = [
            [keyboards.text_button(
                f"{point['n']}. {points.short(point['address'], 34)}",
                {"a": "pt", "n": point["n"], "v": version},
            )]
            for point in shown
        ]
        return keyboards.inline(rows), templates.POINTS_HINT

    if details.get("email_suggestion") and details.get("pending_recipient"):
        return keyboards.inline([[
            keyboards.text_button(f"Да, {details['email_suggestion']}", {"a": "email_yes", "v": version}, "positive"),
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
        )]]), ""
    return None, ""


async def prepare(peer_id: int, reply: str) -> str:
    """Решить, пойдёт ли под ответом клавиатура; вернуть ответ с подсказкой.

    Подсказку дописываем, только когда кнопки клиент действительно увидит,
    и до записи в историю — туда попадает ровно то, что ушло клиенту.
    """
    try:
        keyboard, hint = await for_reply(peer_id)
        markup = await keyboards.for_peer(peer_id, keyboard)
    except Exception:
        logger.exception("Не собрали кнопки для peer_id=%s", peer_id)
        return reply
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


STALE = Press(reply=templates.button_stale(), to_model=True)
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
    await funnel.record(peer_id, f"button:{action}", order_id=order_id, version=payload.get("v"))
    try:
        press = await handler(peer_id, payload)
    except Exception:
        logger.exception("Нажатие %s у peer_id=%s не обработалось, отдаём модели", action, peer_id)
        return TO_MODEL
    if press is STALE:
        await funnel.record(peer_id, "button_stale", order_id=order_id, action=action)
    return press


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
    if not (details.get("ozon_point_id") or details.get("delivery_point")) and draft.delivery_method != "cdek_courier":
        return "Выберите пункт выдачи и одним сообщением пришлите ФИО, телефон и почту — сразу пришлю счёт."
    return templates.ASK_RECIPIENT


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
        return Press(reply=templates.point_chosen(
            address=chosen["address"],
            delivery_cost=fresh.delivery_cost,
            total=fresh.items_total + (fresh.delivery_cost or 0),
            ask_recipient=True,
        ))

    return await _invoice_or(peer_id, chosen_reply)


async def _on_add(peer_id: int, payload: dict) -> Press:
    from app.modules.orders import conversation

    draft = await _draft_at(peer_id, payload)
    item = draft.details.get("upsell_item") if draft else None
    if draft is None or not item:
        return STALE
    if draft.details.get("offer"):
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
        return Press(reply=f"Записала почту {suggestion}.\n{_missing(fresh)}")

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


async def _to_model(peer_id: int, payload: dict) -> Press:
    return TO_MODEL


_HANDLERS = {
    "pt": _on_point,
    "add": _on_add,
    "email_yes": _on_email_yes,
    "email_no": _on_email_no,
    "new_link": _on_new_link,
    "checkout": _on_checkout,
    "offer_ok": _on_offer_ok,
    "repeat": _on_repeat,
    "edit": _to_model,
    "other": _to_model,
}


def register(action: str, handler) -> None:
    """Добавить действие — для «Оформить» постоянного клиента и «Повторить»."""
    _HANDLERS[action] = handler
