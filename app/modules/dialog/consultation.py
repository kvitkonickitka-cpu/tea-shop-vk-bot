"""События консультации и этап воронки хода — для аналитики, без модели-продавца.

`consult_started` — первый ход модели в сессии, если у клиента нет ни
черновика, ни выставленного счёта и сообщение не про посылку в пути.
«Про посылку» решает код: есть оплаченный заказ, ещё не врученный, и текст
похож на вопрос о статусе (`is_status_question`). Модель-продавец в этом не
участвует — её ответ не должен менять счёт консультаций.

`consult_qualified` — в той же сессии бот назвал товар из каталога (по этим
же названиям ставятся кнопки «Взять», `orders/take.py`).

Оба события — один раз на сессию: отметка в `dialog_sessions` ставится
условным UPDATE, кто поставил — тот и пишет событие.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

# Этапы воронки — те же шаги, что в analytics.v_funnel_steps.
CONSULTATION = "consultation"
DRAFT = "draft_created"
DELIVERY = "delivery_quoted"
POINT = "point_chosen"
RECIPIENT = "recipient_set"
INVOICE = "invoice"

_STATUS = re.compile(
    r"(где\s+(мой|моя|мои|наш|наша)?\s*(заказ|посылк|отправлен|чай)"
    r"|трек|отследить|отслеживан|номер\s+отправлен|статус\s+(заказа|посылки|доставки)"
    r"|когда\s+(при[дй]|приед|достав|будет|получ)"
    r"|(пришла|пришёл|пришел|приехал[аи]?|доставлен[аы]?)\s*(ли)?\s*(посылк|заказ)"
    r"|посылк)",
    re.IGNORECASE,
)


def is_status_question(text: str) -> bool:
    return bool(_STATUS.search(text or ""))


def funnel_stage(draft, live_order) -> str:
    """Где клиент на пути к оплате — для расхода Claude по этапам."""
    if live_order is not None:
        return INVOICE
    if draft is None:
        return CONSULTATION
    details = draft.details or {}
    if not draft.delivery_method:
        return DRAFT
    point_needed = {"ozon_pvz": "ozon_point_id", "cdek_pvz": "delivery_point"}.get(draft.delivery_method)
    if point_needed and not details.get(point_needed):
        return DELIVERY
    if not details.get("recipient_name"):
        return POINT
    return RECIPIENT


def open_order_id(draft, live_order) -> int | None:
    if live_order is not None:
        return live_order.id
    if draft is not None:
        value = (draft.details or {}).get("order_id")
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None
    return None


async def has_active_shipment(peer_id: int) -> bool:
    """Оплаченный заказ, который ещё не вручён и не вернулся."""
    from app.modules.orders.models import Order

    try:
        async with get_session_factory()() as session:
            found = (await session.execute(
                select(Order.id).where(
                    Order.peer_id == peer_id,
                    Order.payment_status == "succeeded",
                    Order.delivered_at.is_(None),
                    Order.not_delivered_at.is_(None),
                    Order.status.notin_(("refunded", "canceled")),
                ).limit(1)
            )).first()
    except Exception:
        logger.warning("Не проверили посылки в пути peer_id=%s", peer_id, exc_info=True)
        return False
    return found is not None


async def after_turn(peer_id: int, session_id: int | None, *, had_draft: bool, had_invoice: bool,
                     text: str, reply: str) -> None:
    """После хода модели: отметить начало и квалификацию консультации. Не бросает."""
    if session_id is None:
        return
    try:
        await _after_turn(peer_id, session_id, had_draft=had_draft, had_invoice=had_invoice, text=text, reply=reply)
    except Exception:
        logger.exception("Не отметили консультацию peer_id=%s", peer_id)


async def _after_turn(peer_id: int, session_id: int, *, had_draft: bool, had_invoice: bool,
                      text: str, reply: str) -> None:
    from app.messages import funnel
    from app.modules.dialog import sessions

    session = await sessions.get(session_id)
    if session is None:
        return
    if session.consult_started_at is None:
        if had_draft or had_invoice:
            return
        if is_status_question(text) and await has_active_shipment(peer_id):
            return
        if await sessions.claim_consult_started(session_id):
            await funnel.record(peer_id, "consult_started", source_=funnel.CODE)
    if session.consult_qualified_at is None and reply:
        from app.modules.catalog import service as catalog_service
        from app.modules.orders import take

        named = take.mentioned(reply, catalog_service.load_items())
        if named and await sessions.claim_consult_qualified(session_id):
            await funnel.record(peer_id, "consult_qualified", source_=funnel.CODE,
                                items=[take.display_name(item["name"]) for item in named[:5]])
