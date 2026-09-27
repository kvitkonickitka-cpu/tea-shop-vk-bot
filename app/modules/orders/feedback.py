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

from app.core.database import get_session_factory
from app.messages import manager as manager_messages, templates
from app.modules.dialog import vk_client
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order, OrderFeedback

logger = logging.getLogger(__name__)

FEEDBACK_WINDOW = timedelta(days=14)
CONSENTS = ("yes", "no", "unknown")

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


def prompt_for(order: Order) -> str:
    """Инструкция 1.9 и номер заказа, который модель передаст в инструмент."""
    return (
        _PROMPT.format(composition=templates.composition(order.items or []))
        + f" Номер этого заказа для save_feedback: {order.id}."
    )


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

    if consent == "unknown":
        return (
            "Отзыв записан. Если он хороший — один раз спроси, можно ли "
            "опубликовать его в сообществе, и передай ответ в save_feedback."
        )
    return "Отзыв записан, решение о публикации тоже. Поблагодари клиента коротко."
