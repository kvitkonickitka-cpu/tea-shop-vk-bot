"""Повторные касания после вручения: общие правила и очередь.

Касаний четыре, все продающие: оценка (`feedback.ask_candidates`), «Повторить
заказ?» (`repeat_nudge`), второй шанс (`second_touch`) и реактивация
(`reactivation`). Каждый модуль знает только своё: когда касание созрело,
до какого числа оно ещё уместно («срок годности») и что написать. Всё
остальное — здесь, одним фильтром, а не проверками в каждом касании.

Касание не уходит, если:

- клиент отписан («стоп») или ВК не доставляет ему сообщения;
- у клиента черновик, счёт ждёт оплаты или оплаченный заказ ещё в пути;
- открыт вопрос к менеджеру или менеджер писал в диалог за 48 часов;
- клиент сам писал за 12 часов — разговор идёт;
- по последнему заказу возврат, «не вручено» или жалоба — в этом цикле
  касаний нет;
- после реактивации ещё не было заказа;
- с прошлого касания не прошло `marketing_min_gap_days`.

Отказ фильтра ничего не помечает: касание просто остаётся кандидатом и
уйдёт на следующем тике, когда помеха уйдёт, — пока не истёк его срок
годности. Так «отложить до конца паузы» и «пропустить, если срок вышел»
получаются сами.

Одному клиенту — одно касание за тик, по приоритету: оценка → повтор →
второй шанс → реактивация. Остальные ждут паузу. Отправка — через журнал
«одно событие — одно сообщение», ключ — заказ и тип касания.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, funnel, marketing, templates
from app.messages.models import ClientNotice, ClientPreference, FunnelEvent
from app.modules.dialog import escalation_state, history as dialog_history
from app.modules.dialog.models import ConversationMessage
from app.modules.orders import repository as orders_repository, state
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

# Порядок — приоритет: созрело несколько, уходит первое.
KINDS = (templates.FEEDBACK_ASK, templates.REPEAT_NUDGE, templates.SECOND_TOUCH, templates.REACTIVATION)
NAMES = {
    templates.FEEDBACK_ASK: "оценка",
    templates.REPEAT_NUDGE: "«Повторить заказ?»",
    templates.SECOND_TOUCH: "второй шанс",
    templates.REACTIVATION: "реактивация",
}
# Окна, в которые касанию засчитываются заказ и отписка.
ORDER_WINDOW = timedelta(days=7)
OPTOUT_WINDOW = timedelta(days=2)


@dataclass
class Message:
    text: str
    keyboard: dict | None = None


@dataclass
class Touch:
    """Созревшее касание: кому, по какому заказу, когда и что сказать."""

    kind: str
    peer_id: int
    order: Order
    due: datetime
    expires: datetime
    # Собрать сообщение. None — сказать нечего (нет сорта для предложения),
    # касание не уходит.
    build: Callable[[], Awaitable[Message | None]]


@dataclass
class Decision:
    kind: str
    peer_id: int
    order_id: int
    outcome: str  # sent / blocked / nothing / failed
    reason: str = ""


def aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def ripe(due: datetime, expires: datetime, now: datetime) -> bool:
    return due <= now <= expires


async def sent_at(peer_id: int, kinds=KINDS) -> datetime | None:
    """Когда клиенту в последний раз ушло продающее касание."""
    async with get_session_factory()() as session:
        moment = await session.scalar(
            select(func.max(ClientNotice.sent_at)).where(
                ClientNotice.peer_id == peer_id,
                ClientNotice.event_type.in_(kinds),
                ClientNotice.sent_at.is_not(None),
            )
        )
    return aware(moment)


async def already(order_id: int, kind: str) -> bool:
    return await client_messages.already_sent(client_messages.order_ref(order_id), kind)


async def blocker(peer_id: int, now: datetime) -> str | None:
    """Почему клиенту сейчас нельзя продающее касание. None — можно."""
    if await marketing.is_opted_out(peer_id):
        return "клиент отписан («стоп»)"
    session_factory = get_session_factory()
    async with session_factory() as session:
        preference = await session.get(ClientPreference, peer_id)
        if preference is not None and preference.unreachable_at is not None:
            return "ВК не доставляет клиенту сообщения — ждём, пока напишет сам"

        last_order = (
            await session.execute(
                select(Order)
                .where(Order.peer_id == peer_id, Order.payment_status == orders_repository.PAID)
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        in_transit = (
            await session.execute(
                select(Order.id).where(
                    Order.peer_id == peer_id,
                    Order.payment_status == orders_repository.PAID,
                    Order.delivered_at.is_(None),
                    Order.not_delivered_at.is_(None),
                    Order.status.not_in(("refunded", orders_repository.CANCELED)),
                ).limit(1)
            )
        ).first()
        manager_said = (
            await session.execute(
                select(ConversationMessage.id).where(
                    ConversationMessage.peer_id == peer_id,
                    ConversationMessage.role == "assistant",
                    ConversationMessage.author == dialog_history.AUTHOR_MANAGER,
                    ConversationMessage.created_at
                    > now - timedelta(hours=settings.marketing_quiet_after_manager_hours),
                ).limit(1)
            )
        ).first()
        client_said = (
            await session.execute(
                select(ConversationMessage.id).where(
                    ConversationMessage.peer_id == peer_id,
                    ConversationMessage.role == "user",
                    ConversationMessage.created_at
                    > now - timedelta(hours=settings.marketing_quiet_after_client_hours),
                ).limit(1)
            )
        ).first()

    if await state.get_draft(peer_id) is not None:
        return "у клиента черновик заказа"
    if await orders_repository.live_invoice_order(peer_id) is not None:
        return "счёт ждёт оплаты"
    if in_transit is not None:
        return "оплаченный заказ ещё не вручён"
    if await escalation_state.is_open(peer_id):
        return "открыт вопрос к менеджеру"
    if manager_said is not None:
        return "менеджер писал в диалог за последние 48 часов"
    if client_said is not None:
        return "клиент писал за последние 12 часов — разговор идёт"
    if last_order is not None:
        details = last_order.details or {}
        if last_order.status == "refunded":
            return f"по последнему заказу №{last_order.id} возврат"
        if last_order.not_delivered_at is not None:
            return f"последний заказ №{last_order.id} не вручён"
        if details.get("complaint_at"):
            return f"по последнему заказу №{last_order.id} жалоба"
        reactivated = await sent_at(peer_id, (templates.REACTIVATION,))
        if reactivated is not None and reactivated > aware(last_order.created_at):
            return "после реактивации ждём нового заказа"

    last = await sent_at(peer_id)
    gap = timedelta(days=settings.marketing_min_gap_days)
    if last is not None and now - last < gap:
        return f"пауза между касаниями до {(last + gap).date():%d.%m}"
    return None


async def _candidates(now: datetime, kinds) -> list[Touch]:
    from app.modules.orders import feedback, reactivation, repeat_nudge, second_touch

    providers = {
        templates.FEEDBACK_ASK: (settings.feedback_ask_enabled, feedback.ask_candidates),
        templates.REPEAT_NUDGE: (settings.repeat_nudge_enabled, repeat_nudge.candidates),
        templates.SECOND_TOUCH: (settings.second_touch_enabled, second_touch.candidates),
        templates.REACTIVATION: (settings.reactivation_enabled, reactivation.candidates),
    }
    found: list[Touch] = []
    for kind in KINDS:
        enabled, provider = providers.get(kind, (False, None))
        if not enabled or (kinds is not None and kind not in kinds):
            continue
        try:
            found += await provider(now)
        except Exception:
            logger.exception("Повторные касания: не собрали кандидатов «%s»", kind)
    return found


async def check(
    now: datetime | None = None, *, kinds=None, peer_id: int | None = None, dry: bool = False
) -> dict:
    """Тик расписания: разослать созревшие касания по общим правилам.

    `kinds` и `peer_id` сужают проход (служебная команда и тесты), `dry` —
    только показать решения, ничего не отправляя.
    """
    now = now or datetime.now(timezone.utc)
    if not marketing.in_window(now):
        return {"sent": 0, "skipped": "вне окна продающих сообщений", "decisions": []}
    try:
        get_session_factory()
    except RuntimeError:
        return {"sent": 0, "skipped": "нет базы", "decisions": []}

    touches = [t for t in await _candidates(now, kinds) if peer_id is None or t.peer_id == peer_id]
    by_peer: dict[int, list[Touch]] = {}
    for touch in touches:
        by_peer.setdefault(touch.peer_id, []).append(touch)

    decisions: list[Decision] = []
    sent = 0
    for peer, queue in by_peer.items():
        queue.sort(key=lambda t: (KINDS.index(t.kind), t.due))
        reason = await blocker(peer, now)
        if reason is not None:
            decisions += [Decision(t.kind, peer, t.order.id, "blocked", reason) for t in queue]
            continue
        for touch in queue:
            message = await touch.build()
            if message is None:
                decisions.append(Decision(touch.kind, peer, touch.order.id, "nothing", "нечего предложить"))
                continue
            if dry:
                decisions.append(Decision(touch.kind, peer, touch.order.id, "would_send", message.text))
                break
            ok = await client_messages.send(
                peer_id=peer,
                ref=client_messages.order_ref(touch.order.id),
                event_type=touch.kind,
                text=message.text,
                keyboard=message.keyboard,
                at=now,
            )
            if ok:
                sent += 1
                await funnel.record(peer, f"touch:{touch.kind}", order_id=touch.order.id, at=now)
                logger.info("Касание «%s» по заказу %s отправлено", touch.kind, touch.order.id)
            decisions.append(Decision(touch.kind, peer, touch.order.id, "sent" if ok else "failed"))
            # Одно касание за раз: остальные ждут паузу по общему правилу.
            break
    return {"due": len(touches), "sent": sent, "decisions": decisions}


async def _last_touch(peer_id: int, since: datetime) -> FunnelEvent | None:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(FunnelEvent)
                .where(
                    FunnelEvent.peer_id == peer_id,
                    FunnelEvent.event.like("touch:%"),
                    FunnelEvent.created_at > since,
                )
                .order_by(FunnelEvent.created_at.desc())
                .limit(1)
            )
        ).scalars().first()


async def note_order(peer_id: int, order_id: int, now: datetime | None = None) -> None:
    """Оплаченный заказ в течение 7 дней после касания — засчитать касанию."""
    now = now or datetime.now(timezone.utc)
    try:
        touch = await _last_touch(peer_id, now - ORDER_WINDOW)
        if touch is not None:
            await funnel.record(
                peer_id, "touch_order", order_id=order_id, at=now, touch=touch.event.split(":", 1)[1]
            )
    except Exception:
        logger.exception("Не засчитали заказ %s касанию", order_id)


async def note_opt_out(peer_id: int, now: datetime | None = None) -> None:
    """Отписка в течение 2 дней после касания — тоже результат касания."""
    now = now or datetime.now(timezone.utc)
    try:
        touch = await _last_touch(peer_id, now - OPTOUT_WINDOW)
        if touch is not None:
            await funnel.record(
                peer_id, "touch_optout", order_id=touch.order_id, at=now,
                touch=touch.event.split(":", 1)[1],
            )
    except Exception:
        logger.exception("Не засчитали отписку касанию для peer_id=%s", peer_id)
