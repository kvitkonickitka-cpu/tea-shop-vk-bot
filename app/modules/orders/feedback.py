"""Отзывы о вручённых заказах: инструмент save_feedback и карточка менеджеру.

Отзыв ловит модель: клиент пишет «чай отличный» в ответ на «вручён — будет
здорово, если напишете, как вам чай». Инструмент и инструкция к нему
появляются только у тех, чей заказ вручён за последние
`FEEDBACK_WINDOW` дней, — остальным нечего отзывать, а лишний инструмент
провоцирует модель записывать в отзывы что попало.

Карточка менеджеру (4.14) уходит через очередь уведомлений: при первом
сохранении и когда клиент изменил решение о публикации. Уточнение текста
без смены согласия карточку не повторяет — иначе на «и ещё упаковка
понравилась» менеджер получал бы второй «⭐ Отзыв» по тому же заказу.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import keyboard as keyboards, manager as manager_messages, templates
from app.modules.dialog import vk_client
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderFeedback, OrderRating

logger = logging.getLogger(__name__)

FEEDBACK_WINDOW = timedelta(days=14)
CONSENTS = ("yes", "no", "unknown")
RATINGS = tuple(templates.RATINGS)

TOOL = {
    "name": "save_feedback",
    "description": (
        "Записать отзыв клиента о вручённом заказе: как ему чай, упаковка, "
        "доставка — когда он доволен или делится впечатлением. Жалобу "
        "(не понравился вкус, плохое качество, помятая упаковка, проблема с "
        "доставкой) сюда не записывай — для неё escalate_to_manager. "
        "Повторный вызов по тому же заказу обновляет отзыв: так передаётся "
        "ответ клиента, можно ли опубликовать отзыв."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "integer", "description": "Номер вручённого заказа"},
            "text": {"type": "string", "description": "Отзыв словами клиента"},
            "publish_consent": {
                "type": "string",
                "enum": list(CONSENTS),
                "description": (
                    "Можно ли опубликовать отзыв в сообществе: yes — клиент "
                    "разрешил, no — отказался, unknown — ещё не спрашивали"
                ),
            },
            "rating": {
                "type": "string",
                "enum": list(RATINGS),
                "description": (
                    "Оценка по смыслу отзыва: great — очень понравился, ok — "
                    "нормально, no — не его вкус. Не уверена — не передавай"
                ),
            },
        },
        "required": ["order_id", "text", "publish_consent"],
    },
}

_PROMPT = (
    "У клиента недавно вручён заказ — {composition}. Если он пишет, как ему чай "
    "или заказ, это отзыв: вызови save_feedback с его словами. Поблагодари "
    "одним-двумя предложениями, без шаблонного восторга. Если отзыв хороший — "
    "один раз спроси, можно ли опубликовать его в сообществе, и передай ответ в "
    "save_feedback. Если в отзыве жалоба на вкус, качество, упаковку или "
    "доставку — это жалоба: вызови escalate_to_manager, а не save_feedback. "
    "Если клиент пишет о другом — отвечай как обычно."
)


async def recent_delivered(peer_id: int, now: datetime | None = None) -> Order | None:
    """Последний заказ клиента, вручённый за окно отзывов. Нет — None."""
    now = now or datetime.now(timezone.utc)
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None
    async with session_factory() as session:
        return (
            await session.execute(
                select(Order)
                .where(
                    Order.peer_id == peer_id,
                    Order.payment_status == orders_repository.PAID,
                    Order.delivered_at > now - FEEDBACK_WINDOW,
                    Order.status.not_in(("refunded", orders_repository.CANCELED)),
                )
                .order_by(Order.delivered_at.desc())
                .limit(1)
            )
        ).scalars().first()


_RATED = {
    "great": "Клиент оценил заказ кнопкой «Очень понравился» — попроси пару слов для отзыва.",
    "ok": (
        "Клиент оценил заказ кнопкой «Нормально». Его ответ — консультация: "
        "подбери чай под то, что было бы лучше (крепче, мягче, другой вкус), "
        "опираясь на его покупки."
    ),
    "no": (
        "Клиент оценил заказ кнопкой «Не моё». Выясни, что не подошло, и предложи "
        "другой сорт. Если он жалуется на качество (брак, запах, не тот товар) — "
        "это жалоба: escalate_to_manager с complaint=true."
    ),
}


def prompt_for(order: Order, rating: str | None = None) -> str:
    """Инструкция 1.9 и номер заказа, который модель передаст в инструмент."""
    return (
        _PROMPT.format(composition=templates.composition(order.items or []))
        + f" Номер этого заказа для save_feedback: {order.id}."
        + (f" {_RATED[rating]}" if rating in _RATED else "")
    )


async def rating_of(order_id: int) -> str | None:
    try:
        async with get_session_factory()() as session:
            row = await session.get(OrderRating, order_id)
    except Exception:
        return None
    return row.rating if row is not None else None


async def rate(peer_id: int, order_id: int, rating: str, source: str, now: datetime | None = None) -> bool:
    """Записать оценку заказа. True — она новая или изменилась."""
    now = now or datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        previous = await session.get(OrderRating, order_id)
        before = previous.rating if previous is not None else None
        statement = insert(OrderRating).values(
            order_id=order_id, peer_id=peer_id, rating=rating, source=source,
        ).on_conflict_do_update(
            index_elements=[OrderRating.order_id],
            set_={"rating": rating, "source": source, "updated_at": now},
        )
        await session.execute(statement)
        await session.commit()
    logger.info("Оценка заказа %s: %s (%s)", order_id, rating, source)
    if before != rating:
        from app.messages import funnel

        await funnel.record(peer_id, "rated", order_id=order_id, source_=source, value=rating)
    return before != rating


async def mark_complaint(peer_id: int, now: datetime | None = None) -> int | None:
    """Жалоба на недавно вручённый заказ — в этом цикле повторных касаний нет."""
    order = await recent_delivered(peer_id, now)
    if order is None:
        return None
    async with get_session_factory()() as session:
        fresh = await session.get(Order, order.id)
        details = dict(fresh.details or {})
        details["complaint_at"] = (now or datetime.now(timezone.utc)).isoformat()
        fresh.details = details
        await session.commit()
    logger.info("Заказ %s: жалоба клиента", order.id)
    return order.id


# --- оценка кнопками через несколько дней после вручения -------------------


def rate_keyboard(order_id: int) -> dict | None:
    return keyboards.inline([[
        keyboards.text_button(
            label, {"a": "rate", "o": order_id, "r": code, "t": templates.FEEDBACK_ASK},
            "positive" if code == "great" else "secondary",
        )
        for code, label in templates.RATINGS.items()
    ]])


async def ask_candidates(now: datetime):
    """Заказы, по которым пора спросить «Как вам чай?»."""
    from app.modules.orders import retention

    from app.modules.analytics import service as analytics

    after = timedelta(days=settings.feedback_ask_after_days)
    shelf = timedelta(days=settings.feedback_ask_shelf_days)
    test_filter = analytics.test_order_filter(await analytics.test_peer_ids())
    async with get_session_factory()() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    test_filter,
                    Order.payment_status == orders_repository.PAID,
                    Order.delivered_at <= now - after,
                    Order.delivered_at >= now - after - shelf,
                    Order.not_delivered_at.is_(None),
                    Order.status.not_in(("refunded", orders_repository.CANCELED)),
                )
            )
        ).scalars().all()
        reviewed = set((await session.execute(
            select(OrderFeedback.order_id).where(OrderFeedback.order_id.in_([o.id for o in orders]))
        )).scalars().all()) if orders else set()
        rated = set((await session.execute(
            select(OrderRating.order_id).where(OrderRating.order_id.in_([o.id for o in orders]))
        )).scalars().all()) if orders else set()

    touches = []
    for order in orders:
        # Отзыв или оценка уже есть — спрашивать нечего.
        if order.id in reviewed or order.id in rated:
            continue
        if await retention.already(order.id, templates.FEEDBACK_ASK):
            continue
        due = retention.aware(order.delivered_at) + after
        touches.append(retention.Touch(
            kind=templates.FEEDBACK_ASK, peer_id=order.peer_id, order=order,
            due=due, expires=due + shelf, build=_ask_builder(order),
        ))
    return touches


def _ask_builder(order: Order):
    from app.modules.orders import retention, take

    async def build():
        names = {item.get("name") for item in order.items or [] if item.get("name")}
        single = take.display_name(next(iter(names))) if len(names) == 1 else ""
        return retention.Message(templates.feedback_ask(order, single), rate_keyboard(order.id))
    return build


async def save(peer_id: int, tool_input: dict, now: datetime | None = None) -> str:
    """Записать или обновить отзыв. Возвращает результат для модели."""
    now = now or datetime.now(timezone.utc)
    text = (tool_input.get("text") or "").strip()
    consent = tool_input.get("publish_consent") or "unknown"
    if consent not in CONSENTS:
        consent = "unknown"
    try:
        order_id = int(tool_input.get("order_id"))
    except (TypeError, ValueError):
        order_id = None
    if not text:
        return "Отзыв пустой — запиши его словами клиента."

    order = await recent_delivered(peer_id, now)
    if order is None or (order_id is not None and order_id != order.id):
        # Номер от модели проверяем: отзыв к чужому или невручённому заказу
        # менеджеру ни к чему.
        return (
            "Вручённого за последние две недели заказа с таким номером у клиента "
            "нет — отзыв не записан. Поблагодари клиента и отвечай как обычно."
        )

    session_factory = get_session_factory()
    async with session_factory() as session:
        previous = await session.get(OrderFeedback, order.id)
        before = previous.publish_consent if previous is not None else None
        statement = insert(OrderFeedback).values(
            order_id=order.id, peer_id=peer_id, text=text, publish_consent=consent,
        ).on_conflict_do_update(
            index_elements=[OrderFeedback.order_id],
            set_={"text": text, "publish_consent": consent, "updated_at": now},
        )
        await session.execute(statement)
        await session.commit()

    rating = tool_input.get("rating")
    if rating in RATINGS:
        await rate(peer_id, order.id, rating, "text", now)

    if before is None or before != consent:
        await manager_messages.notify(
            manager_messages.FEEDBACK,
            templates.manager_feedback(
                order.id, html.escape(text), consent, vk_client.dialog_link(peer_id)
            ),
            order_id=order.id,
            peer_id=peer_id,
        )
    logger.info("Отзыв по заказу %s записан (публикация: %s)", order.id, consent)
    if before is None:
        from app.messages import funnel

        await funnel.record(peer_id, "review_saved", order_id=order.id, consent=consent)

    if consent == "unknown":
        return (
            "Отзыв записан. Если он хороший — один раз спроси, можно ли "
            "опубликовать его в сообществе, и передай ответ в save_feedback."
        )
    return "Отзыв записан, решение о публикации тоже. Поблагодари клиента коротко."
