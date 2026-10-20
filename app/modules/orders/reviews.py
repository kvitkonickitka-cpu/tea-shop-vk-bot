"""Отзывы после покупки (C7, флаг review_flow).

Оценка через 3 дня после вручения остаётся (`feedback.ask_candidates`), но
кнопками 1–5 ⭐. Дальше решает код:

- **4–5** — просьба написать пару слов (`review_requested`). Текст ловит
  модель инструментом `save_feedback`; согласие на публикацию — кнопками
  «С именем из ВК» / «Анонимно» / «Не публиковать» (`review_consent`).
  Согласие есть — карточка менеджеру с готовым текстом; публикация только
  после его кнопки в Telegram (`review_published`). Модерация обязательна:
  сам бот ничего не публикует.
- **1–3** — сразу вопрос менеджеру с меткой жалобы и составом заказа
  (`complaint_from_rating`), клиенту — извинение и когда ответят.
  Продающие касания на паузе, пока менеджер не ответит (`complaint_closed_at`).

Никаких выдуманных отзывов и наград за оценку. Как публиковать —
`REVIEW_PUBLISH_MODE`: `manual` (менеджер руками, кнопка «Опубликовал
вручную») или `wall` (бот на стену сообщества, если у токена есть право wall).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import flags, worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import funnel, keyboard as keyboards, templates
from app.modules.orders.models import Order, OrderFeedback, OrderRating

logger = logging.getLogger(__name__)

STARS = (1, 2, 3, 4, 5)
CALLBACK_PUBLISH = "rv:pub:"
CALLBACK_DONE = "rv:done:"


def rating_code(score: int) -> str:
    """Оценка 1–5 → прежние три слова для аналитики и «Не моё» в касаниях."""
    return "great" if score >= 4 else "ok" if score == 3 else "no"


def star_keyboard(order_id: int) -> dict | None:
    return keyboards.inline([[
        keyboards.text_button(f"{n} ⭐", {"a": "rate", "o": order_id, "r": str(n), "t": templates.FEEDBACK_ASK},
                              "positive" if n >= 4 else "secondary")
        for n in STARS
    ]])


def consent_keyboard(order_id: int) -> dict | None:
    return keyboards.inline([[
        keyboards.text_button(label, {"a": "rv_consent", "o": order_id, "c": code},
                              "positive" if code != "no" else "secondary")
        for code, label in templates.REVIEW_VARIANTS.items()
    ]])


async def enabled(peer_id: int) -> bool:
    return await flags.enabled_for(flags.REVIEW_FLOW, peer_id)


async def on_star(peer_id: int, order: Order, score: int) -> str:
    """Оценка 1–5 кнопкой: записать и ответить по ветке."""
    from app.modules.orders import feedback

    await feedback.rate(peer_id, order.id, rating_code(score), "button")
    async with get_session_factory()() as session:
        row = await session.get(OrderRating, order.id)
        if row is not None:
            row.score = score
            await session.commit()
    if score >= 4:
        await funnel.record(peer_id, "review_requested", order_id=order.id, value=score)
        return templates.review_request()
    await _complaint(peer_id, order, score)
    return templates.complaint_apology(worktime.manager_back_phrase() if not worktime.is_working() else "сегодня")


async def _complaint(peer_id: int, order: Order, score: int) -> None:
    from app.modules.orders import conversation

    composition = templates.composition(order.items or [])
    await conversation._execute_escalate_to_manager(peer_id, {
        "question": templates.manager_complaint_from_rating(order, score, composition),
        "reason": f"оценка {score} из 5 после вручения",
        "complaint": True,
    })
    # Жалоба из оценки закрывается ответом менеджера: до него продающих касаний нет.
    async with get_session_factory()() as session:
        fresh = await session.get(Order, order.id)
        fresh.details = {**(fresh.details or {}), "complaint_source": "rating",
                         "complaint_at": datetime.now(timezone.utc).isoformat()}
        await session.commit()
    await funnel.record(peer_id, "complaint_from_rating", order_id=order.id, value=score)


async def close_complaints(peer_id: int) -> int:
    """Менеджер ответил — жалобы из оценки закрыты, касания снова можно."""
    closed = 0
    async with get_session_factory()() as session:
        orders = (await session.execute(select(Order).where(Order.peer_id == peer_id))).scalars().all()
        for order in orders:
            details = order.details or {}
            if details.get("complaint_source") == "rating" and not details.get("complaint_closed_at"):
                order.details = {**details, "complaint_closed_at": datetime.now(timezone.utc).isoformat()}
                closed += 1
        await session.commit()
    return closed


async def on_consent(peer_id: int, order_id: int, variant: str) -> str | None:
    """Кнопка согласия на публикацию. None — кнопка не к этому отзыву."""
    if variant not in templates.REVIEW_VARIANTS:
        return None
    async with get_session_factory()() as session:
        row = await session.get(OrderFeedback, order_id)
        if row is None or row.peer_id != peer_id:
            return None
        row.publish_consent = variant
        row.updated_at = datetime.now(timezone.utc)
        text = row.text
        await session.commit()
        score = (await session.execute(select(OrderRating.score).where(OrderRating.order_id == order_id))).scalar()
    await funnel.record(peer_id, "review_consent", order_id=order_id, variant=variant)
    if variant != "no":
        await _card(peer_id, order_id, text, variant, score)
    return templates.review_consent_saved(variant)


def _wall() -> bool:
    return (settings.review_publish_mode or "").strip() == "wall"


async def _card(peer_id: int, order_id: int, text: str, variant: str, score) -> None:
    """Карточка менеджеру с кнопкой — публикация только по ней."""
    from app.messages import manager as manager_messages
    from app.modules.dialog import telegram_client, vk_client

    card = templates.manager_review_card(order_id, html.escape(text), variant, score, vk_client.dialog_link(peer_id),
                                         wall=_wall())
    label, data = (("Опубликовать", f"{CALLBACK_PUBLISH}{order_id}") if _wall()
                   else ("Опубликовал вручную", f"{CALLBACK_DONE}{order_id}"))
    try:
        await telegram_client.call("sendMessage", {
            "chat_id": settings.telegram_manager_chat_id, "text": card, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": data}]]},
        })
    except Exception:
        logger.exception("Карточку отзыва по заказу %s с кнопкой не отправили — уходит текстом", order_id)
        await manager_messages.notify(manager_messages.FEEDBACK, card, order_id=order_id, peer_id=peer_id)


async def publish(order_id: int, how: str) -> str:
    """Менеджер нажал кнопку: wall — бот публикует, manual — отметка «опубликовал сам»."""
    async with get_session_factory()() as session:
        row = await session.get(OrderFeedback, order_id)
        if row is None or row.publish_consent not in ("named", "anonymous"):
            return "Отзыва с согласием на публикацию нет."
        if row.published_at is not None:
            return "Уже опубликован."
        peer_id, text, variant = row.peer_id, row.text, row.publish_consent
        score = (await session.execute(select(OrderRating.score).where(OrderRating.order_id == order_id))).scalar()
    if how == "wall":
        from app.modules.dialog import vk_client

        author = "покупатель"
        if variant == "named":
            author = await vk_client.first_name(peer_id) or "покупатель"
        await vk_client.wall_post(templates.review_publication(text, score or 0, author))
    async with get_session_factory()() as session:
        row = await session.get(OrderFeedback, order_id)
        row.published_at = datetime.now(timezone.utc)
        row.published_how = how
        await session.commit()
    await funnel.record(peer_id, "review_published", order_id=order_id, how=how, source_=funnel.MANAGER)
    return "Опубликовано ✅" if how == "wall" else "Отмечено: опубликовано вручную ✅"


async def handle_callback(callback: dict) -> bool:
    """Кнопка под карточкой отзыва в Telegram. Только из чата менеджера или Ops."""
    from app.modules.dialog import telegram_client

    data = str(callback.get("data") or "")
    if not data.startswith(("rv:pub:", "rv:done:")):
        return False
    chat = str(((callback.get("message") or {}).get("chat") or {}).get("id"))
    allowed = {str(settings.telegram_manager_chat_id).strip(), str(settings.telegram_ops_chat_id).strip()} - {""}
    if chat not in allowed:
        return False
    order_id = int(data.rsplit(":", 1)[1])
    how = "wall" if data.startswith(CALLBACK_PUBLISH) and _wall() else "manual"
    try:
        answer = await publish(order_id, how)
    except Exception:
        logger.exception("Публикация отзыва по заказу %s не удалась", order_id)
        answer = "Не получилось — подробности в логе бота."
    try:
        await telegram_client.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": answer})
        if answer.endswith("✅"):
            message = callback.get("message") or {}
            await telegram_client.call("editMessageReplyMarkup", {
                "chat_id": message["chat"]["id"], "message_id": message["message_id"],
                "reply_markup": {"inline_keyboard": []}})
    except Exception:
        logger.warning("Ответ на кнопку отзыва не ушёл", exc_info=True)
    return True
