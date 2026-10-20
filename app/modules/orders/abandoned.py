"""Брошенный черновик или счёт: одно касание через ~20 часов (C3, флаг abandoned_touch).

Первое «заказ ждёт вас» шлёт `draft_nudge` через 3 часа. Это — второе и
последнее: на следующий день, тем, кто так и не оплатил, в том числе после
истёкшего счёта (тогда черновик возвращён со всем составом и номером заказа).

Правила:
- черновик с товарами, последнее действие — реплика бота старше
  `abandoned_touch_after_hours` (клиент после неё не писал) и не старше
  `abandoned_touch_max_age_hours`;
- денег по заказу нет, вопрос у менеджера не открыт, менеджер не писал за 48 ч;
- не «стоп», ВК доставляет, согласие на ПДн есть;
- только в окне продающих сообщений (10:00–21:00, тихие часы — никогда);
- пауза между продающими касаниями (`MARKETING_MIN_GAP_DAYS`) — кроме
  трёхчасового напоминания по тому же черновику: это одна попытка вернуть заказ;
- одно на черновик: ключ — время начала черновика.

Кнопка «Оформить» — тот же `checkout`: код пересчитывает цены, наличие и
доставку и только тогда выставляет счёт. Оплата в течение 7 дней после
касания — событие `abandoned_touch_order` (`note_order`, из журнала воронки).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core import flags
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, funnel, keyboard as keyboards, marketing, templates
from app.modules.consent import service as consent
from app.modules.dialog import escalation_state, history as dialog_history
from app.modules.dialog.models import ConversationMessage
from app.modules.orders import draft_nudge, retention
from app.modules.orders.models import Order, OrderDraftRow

logger = logging.getLogger(__name__)

ORDER_WINDOW = timedelta(days=7)


async def _last_message(session, peer_id: int):
    return (await session.execute(
        select(ConversationMessage.role, ConversationMessage.author, ConversationMessage.created_at)
        .where(ConversationMessage.peer_id == peer_id)
        .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(1)
    )).first()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def _blocked(peer_id: int, now: datetime, started: datetime) -> str | None:
    if await escalation_state.is_open(peer_id):
        return "вопрос у менеджера"
    if await marketing.is_opted_out(peer_id) or await marketing.is_unreachable(peer_id):
        return "стоп или ВК не доставляет"
    if not await consent.has_pd(peer_id):
        return "нет согласия на ПДн"
    async with get_session_factory()() as session:
        manager = (await session.execute(
            select(ConversationMessage.id).where(
                ConversationMessage.peer_id == peer_id, ConversationMessage.author == dialog_history.AUTHOR_MANAGER,
                ConversationMessage.created_at > now - timedelta(hours=settings.marketing_quiet_after_manager_hours),
            ).limit(1))).first()
    if manager is not None:
        return "менеджер писал за 48 ч"
    last = await retention.sent_at(peer_id, (*retention.KINDS, templates.ABANDONED_TOUCH))
    if last is not None and now - last < timedelta(days=settings.marketing_min_gap_days):
        return "пауза между касаниями"
    return None


async def check(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not marketing.in_window(now):
        return {"skipped": "вне окна продающих сообщений"}
    after = timedelta(hours=settings.abandoned_touch_after_hours)
    max_age = timedelta(hours=settings.abandoned_touch_max_age_hours)
    async with get_session_factory()() as session:
        rows = (await session.execute(select(OrderDraftRow).where(
            OrderDraftRow.stage.in_(("awaiting_delivery", "awaiting_confirmation")),
            OrderDraftRow.updated_at >= now - max_age,
        ))).scalars().all()
        candidates = []
        for row in rows:
            if not row.items:
                continue
            started = draft_nudge._started_at(row)
            last = await _last_message(session, row.peer_id)
            if last is None or last.role != "assistant":
                continue  # клиент написал последним — ему отвечают, а не напоминают
            quiet_for = now - _aware(last.created_at)
            if quiet_for < after or quiet_for > max_age:
                continue
            order_id = (row.details or {}).get("order_id")
            if order_id:
                paid = (await session.execute(select(Order.payment_status).where(Order.id == order_id))).scalar()
                if paid == "succeeded":
                    continue
            candidates.append((row, started))
    sent = 0
    for row, started in candidates:
        if not await flags.enabled_for(flags.ABANDONED_TOUCH, row.peer_id):
            continue
        ref = draft_nudge.draft_ref(row.peer_id, started)
        if await client_messages.already_sent(ref, templates.ABANDONED_TOUCH):
            continue
        reason = await _blocked(row.peer_id, now, started)
        if reason:
            logger.info("peer_id=%s: касание по брошенному заказу отложено — %s", row.peer_id, reason)
            continue
        total = float(row.items_total or 0) + float(row.delivery_cost or 0)
        delivery = draft_nudge._delivery_phrase(row) if row.delivery_method else ""
        keyboard = keyboards.inline([[keyboards.text_button(
            "Оформить", {"a": "checkout", "v": (row.details or {}).get("version")}, "positive")]])
        if await client_messages.send(
            peer_id=row.peer_id, ref=ref, event_type=templates.ABANDONED_TOUCH,
            text=templates.abandoned_touch(list(row.items or []), total=total, delivery=delivery), keyboard=keyboard,
        ):
            await funnel.record(row.peer_id, "abandoned_touch", order_id=(row.details or {}).get("order_id"),
                                source_=funnel.REMINDER, at=now)
            sent += 1
    return {"кандидатов": len(candidates), "отправлено": sent}


async def note_order(peer_id: int, order_id: int | None, now: datetime | None = None) -> None:
    """Оплата в течение 7 дней после касания — засчитать касанию. Не бросает."""
    from app.messages.models import FunnelEvent

    now = now or datetime.now(timezone.utc)
    try:
        async with get_session_factory()() as session:
            touched = (await session.execute(select(FunnelEvent.id).where(
                FunnelEvent.peer_id == peer_id, FunnelEvent.event == "abandoned_touch",
                FunnelEvent.created_at > now - ORDER_WINDOW).limit(1))).first()
        if touched is not None:
            await funnel.record(peer_id, "abandoned_touch_order", order_id=order_id, source_=funnel.YOOKASSA)
    except Exception:
        logger.exception("Не засчитали оплату касанию по брошенному заказу peer_id=%s", peer_id)
