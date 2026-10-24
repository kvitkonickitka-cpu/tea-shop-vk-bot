"""Обезличить покупателя по отзыву согласия — командой менеджера.

    scripts/api.sh 'clients/<vk_id>/anonymize'           пробный прогон: что изменится
    scripts/api.sh 'clients/<vk_id>/anonymize?apply=1'   применить

Что убираем: ФИО, телефон, почту и адрес в заказах и попытках оплаты,
черновик, переписку и входящие (в них личное словами), значения меток
(`pii_vault`), тексты вопросов менеджеру, уведомлений и отзывов.

Что оставляем: суммы, состав, статусы, коды маркировки, чеки и их номера —
их срок хранения задаёт закон (бухучёт, 54-ФЗ), — а ещё журнал согласий
(доказательство) и отметку об отписке (иначе рассылка вернётся).

Заказ, который оплачен и ещё не вручён или ждёт закрывающего чека, не
трогаем без `force=1`: перевозчику нужен получатель, а чеку — почта.
Сделать это руками — задача менеджера, команда только скажет, какие это заказы.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select, update

from app.core.client_key import client_key
from app.core.config import settings
from app.core.database import get_session_factory
from app.privacy import detect

logger = logging.getLogger(__name__)

ERASED = "[обезличено]"
# Ключи с личным где бы то ни было в details и snapshot. Общих «name» и
# «phone» здесь нет: «name» — это и название товара в составе, а составы
# остаются. Получатель витрины и предложение «как в прошлый раз» уходят целиком.
_KEYS = {
    "recipient_name", "recipient_phone", "recipient_email", "pending_recipient", "email_suggestion",
    "storefront_recipient", "offer", "vk_order_address", "storefront_street", "shown_query", "point_query",
}
_COURIER = "cdek_courier"


def _scrub_text(text: str) -> str:
    text = detect.EMAIL.sub(ERASED, text)
    return detect.PHONE.sub(lambda m: ERASED if detect.normalize_phone(m.group(0)) else m.group(0), text)


def scrub(value, *, courier: bool = False):
    """Копия значения без личного. Структура и все прочие поля — как были."""
    if isinstance(value, dict):
        courier = courier or value.get("delivery_method") == _COURIER
        result = {}
        for key, item in value.items():
            if item and (key in _KEYS or (key == "address" and courier)):
                result[key] = ERASED
            else:
                result[key] = scrub(item, courier=courier)
        return result
    if isinstance(value, list):
        return [scrub(item, courier=courier) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _in_flight(order) -> bool:
    """Оплачен, а посылка или закрывающий чек ещё впереди: без данных они сорвутся."""
    if order.payment_status != "succeeded" or order.status in ("refunded", "canceled"):
        return False
    if order.delivered_at is None and order.not_delivered_at is None:
        return True
    return bool(settings.settlement_receipt_enabled and order.delivered_at is not None
                and order.settlement_receipt_id is None)


async def anonymize(peer_id: int, *, apply: bool = False, force: bool = False) -> dict:
    from app.messages.models import ManagerNotification
    from app.modules.dialog.models import ConversationMessage, Escalation, InboundMessage
    from app.modules.orders.models import Order, OrderDraftRow, OrderFeedback, OrderPayment
    from app.privacy import vault
    from app.privacy.models import PiiEntry

    key = client_key(peer_id)
    report: dict = {"vk_user_id": peer_id, "применено": apply}
    async with get_session_factory()() as session:
        orders = list((await session.execute(select(Order).where(Order.peer_id == peer_id).order_by(Order.id))).scalars())
        skipped = [order.id for order in orders if _in_flight(order) and not force]
        touched = [order for order in orders if order.id not in skipped]
        payments = list((await session.execute(
            select(OrderPayment).where(OrderPayment.order_id.in_([o.id for o in touched] or [0]))
        )).scalars())
        changed_orders = [o for o in touched if scrub(o.details or {}) != (o.details or {})]
        changed_payments = [p for p in payments if p.snapshot and scrub(p.snapshot) != p.snapshot]

        counts = {
            "переписка": await _count(session, ConversationMessage, ConversationMessage.peer_id == peer_id),
            "входящие": await _count(session, InboundMessage, InboundMessage.peer_id == peer_id),
            "метки": await _count(session, PiiEntry, PiiEntry.client_key == key),
            "черновик": await _count(session, OrderDraftRow, OrderDraftRow.peer_id == peer_id),
            "вопросы менеджеру": await _count(session, Escalation, Escalation.peer_id == peer_id),
            "уведомления менеджеру": await _count(session, ManagerNotification, ManagerNotification.peer_id == peer_id),
            "отзывы": await _count(session, OrderFeedback, OrderFeedback.peer_id == peer_id),
        }
        report.update({
            "заказы: обезличить": [o.id for o in changed_orders],
            "попытки оплаты: обезличить": len(changed_payments),
            "заказы в пути — не тронуты (force=1 — всё равно)": skipped,
            **counts,
            "остаются": "суммы, состав, статусы, коды маркировки, чеки, журнал согласий, отписка",
        })
        if not apply:
            return report

        for order in changed_orders:
            order.details = scrub(order.details or {})
        for payment in changed_payments:
            payment.snapshot = scrub(payment.snapshot)
        await session.execute(delete(ConversationMessage).where(ConversationMessage.peer_id == peer_id))
        await session.execute(delete(InboundMessage).where(InboundMessage.peer_id == peer_id))
        await session.execute(delete(OrderDraftRow).where(OrderDraftRow.peer_id == peer_id))
        await session.execute(delete(PiiEntry).where(PiiEntry.client_key == key))
        await session.execute(update(Escalation).where(Escalation.peer_id == peer_id).values(question=ERASED, reason=ERASED))
        await session.execute(
            update(ManagerNotification).where(ManagerNotification.peer_id == peer_id).values(payload=ERASED)
        )
        await session.execute(update(OrderFeedback).where(OrderFeedback.peer_id == peer_id).values(text=ERASED))
        await session.commit()
    vault.forget_cache()
    logger.info("peer_id=%s обезличен: заказов %s, пропущено в пути %s", peer_id, len(changed_orders), len(skipped))
    return report


async def _count(session, model, condition) -> int:
    from sqlalchemy import func

    return int(await session.scalar(select(func.count()).select_from(model).where(condition)) or 0)

