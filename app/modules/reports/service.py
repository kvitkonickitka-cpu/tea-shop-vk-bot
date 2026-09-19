from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.dialog import claude_client, telegram_client, vk_client
from app.modules.dialog.models import (
    Conversation,
    ConversationMessage,
    DialogReport,
    Escalation,
)
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

# Потолок на объём переписки в одном отчёте: диалог может тянуться неделями,
# а пересказывать имеет смысл только то, что случилось после прошлого отчёта.
_MAX_MESSAGES = 200
_MAX_MESSAGE_CHARS = 1000

_ROLE_LABELS = {"user": "клиент", "assistant": "бот"}


async def send_pending_reports() -> dict:
    """Рассылает мини-отчёты по диалогам, которые замолчали.

    Вызывается по таймеру, а не из обработки сообщения: у вебхука VK около
    восьми секунд на всё про всё, и запрос к Claude ради отчёта в этот бюджет
    не влезает. Здесь спешки нет.
    """
    if not settings.telegram_reports_chat_id:
        return {"skipped": "TELEGRAM_REPORTS_CHAT_ID не задан"}

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {"skipped": "база недоступна"}

    idle_before = datetime.now(timezone.utc) - timedelta(minutes=settings.dialog_report_idle_minutes)

    async with session_factory() as session:
        # Диалог попадает в выборку, если давно молчит И с прошлого отчёта в
        # нём успели появиться новые сообщения. Второе условие позволяет
        # клиенту вернуться через неделю и получить отдельный отчёт, а не
        # остаться навсегда «уже отчитанным».
        rows = (
            await session.execute(
                select(Conversation, DialogReport.reported_at)
                .outerjoin(DialogReport, DialogReport.peer_id == Conversation.peer_id)
                .where(Conversation.last_message_at < idle_before)
                .where(
                    or_(
                        DialogReport.reported_at.is_(None),
                        DialogReport.reported_at < Conversation.last_message_at,
                    )
                )
                .order_by(Conversation.last_message_at)
                .limit(settings.dialog_report_batch_limit)
            )
        ).all()

    sent = 0
    empty = 0
    failed = 0
    for conversation, reported_at in rows:
        try:
            if await _report_one(session_factory, conversation, reported_at):
                sent += 1
            else:
                empty += 1
        except Exception:
            # Отметку не ставим — значит следующий запуск попробует снова.
            logger.exception("Не удалось отправить отчёт по диалогу peer_id=%s", conversation.peer_id)
            failed += 1

    return {"candidates": len(rows), "sent": sent, "empty": empty, "failed": failed}


async def _report_one(session_factory, conversation: Conversation, reported_at: datetime | None) -> bool:
    """Отправляет отчёт по одному диалогу. False — отчитываться было нечем."""

    def _since(column):
        # Для первого отчёта берём диалог целиком. Отсекать по started_at
        # нельзя: шапка диалога и первое сообщение создаются одним и тем же
        # мгновением, и строгое «позже» выбрасывало первое сообщение — а в
        # коротком диалоге это всё, что было.
        return column > reported_at if reported_at is not None else None

    async with session_factory() as session:
        messages_stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.peer_id == conversation.peer_id)
            .order_by(ConversationMessage.id)
            .limit(_MAX_MESSAGES)
        )
        orders_stmt = select(Order).where(Order.peer_id == conversation.peer_id)
        escalations_stmt = select(Escalation).where(Escalation.peer_id == conversation.peer_id)

        if reported_at is not None:
            messages_stmt = messages_stmt.where(_since(ConversationMessage.created_at))
            orders_stmt = orders_stmt.where(_since(Order.created_at))
            escalations_stmt = escalations_stmt.where(_since(Escalation.created_at))

        messages = (await session.execute(messages_stmt)).scalars().all()
        orders = (await session.execute(orders_stmt)).scalars().all()
        escalations = (await session.execute(escalations_stmt)).scalars().all()

    if not messages:
        # Сообщений с прошлого отчёта нет — пересказывать нечего, но отметку
        # ставим, иначе диалог будет всплывать в выборке на каждом запуске.
        await _mark_reported(session_factory, conversation.peer_id)
        return False

    summary = await claude_client.generate_dialog_report(
        _build_transcript(conversation, messages, orders, escalations)
    )

    await telegram_client.send_message(
        _build_telegram_message(conversation, messages, summary),
        chat_id=settings.telegram_reports_chat_id,
    )

    # Только после успешной отправки: упавший отчёт должен повториться, а не
    # потеряться — ровно та же логика, что у отметки обработанных событий VK.
    await _mark_reported(session_factory, conversation.peer_id)
    return True


def _build_transcript(conversation, messages, orders, escalations) -> str:
    lines = [f"Переписка с клиентом, сообщений: {len(messages)}", "", "--- переписка ---"]
    for message in messages:
        role = _ROLE_LABELS.get(message.role, message.role)
        text = message.content[:_MAX_MESSAGE_CHARS]
        lines.append(f"{role}: {text}")

    lines += ["", "--- что произошло по делу ---"]
    if orders:
        for order in orders:
            items = ", ".join(f"{i.get('name')} x{i.get('quantity')}" for i in order.items)
            lines.append(f"Оформлен заказ: {items}. Итого {order.total} руб.")
    else:
        lines.append("Заказов не оформлено.")

    if escalations:
        for escalation in escalations:
            status = "менеджер ответил" if escalation.resolved_at else "ответа менеджера ещё нет"
            lines.append(f"Вопрос передан менеджеру ({status}): {escalation.question}")
    else:
        lines.append("Менеджера не подключали.")

    return "\n".join(lines)


def _build_telegram_message(conversation, messages, summary: str) -> str:
    return (
        "<b>Диалог завершён</b>\n"
        f"{html.escape(summary.strip())}\n\n"
        f"Сообщений: {len(messages)}\n"
        f"{vk_client.dialog_link(conversation.peer_id)}"
    )


async def _mark_reported(session_factory, peer_id: int) -> None:
    async with session_factory() as session:
        await session.merge(DialogReport(peer_id=peer_id, reported_at=datetime.now(timezone.utc)))
        await session.commit()
