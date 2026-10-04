"""Брошенный черновик: одно напоминание «заказ ждёт вас».

Клиент выбрал чай или даже услышал цену доставки — и замолчал. Часто это
не «передумал», а «отвлёкся»: одно сообщение через несколько часов
возвращает часть таких заказов. Два и больше — уже навязчивость, поэтому
правила жёсткие:

- только до счёта: черновик с номером заказа уже ведут напоминания об
  оплате, второй голос там лишний;
- тишина дольше `draft_nudge_after_hours` после последней реплики бота, и
  последняя реплика — наша: если клиент написал, а ответа нет, напоминать
  ему о заказе странно. Черновик из заказа «Товаров» — через
  `storefront_draft_nudge_after_minutes` (`STOREFRONT_EARLY_NUDGE_ENABLED`):
  клиент сам нажал «Оформить» и горячее всех. Пункт не выбран — вместо
  «Оформить?» вопрос о месте и кнопка геопозиции;
- черновику не больше `draft_nudge_max_age_hours`: через неделю «заказ ждёт
  вас» читается как рассылка;
- вопрос у менеджера открыт или менеджер писал после начала черновика —
  разговор ведёт человек, бот в него не встревает;
- клиент не отписывался («стоп»);
- только в окне продающих сообщений (10:00–21:00 по Москве). Позже —
  утром: тик в 10:00 найдёт тот же черновик, если он ещё не устарел;
- одно на черновик, никогда больше: ключ журнала отправок выводится из
  времени начала черновика, и повторно его не занять.

Запись в журнале `client_notices` с типом `draft_nudge_sent` и есть
событие: кто, по какому черновику, когда.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core import worktime
from app.core.config import free_delivery_threshold, settings
from app.core.database import get_session_factory
from app.messages import client as client_messages, keyboard as keyboards, marketing, templates
from app.modules.catalog import service as catalog_service
from app.modules.dialog import escalation_state, history as dialog_history
from app.modules.dialog.models import ConversationMessage
from app.modules.orders.models import OrderDraftRow

logger = logging.getLogger(__name__)

_STAGES = ("awaiting_delivery", "awaiting_confirmation")


def _started_at(row: OrderDraftRow) -> datetime:
    raw = (row.details or {}).get("started_at")
    if raw:
        try:
            moment = datetime.fromisoformat(raw)
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # Черновики, заведённые до появления отметки: `updated_at` двигается с
    # каждой правкой, но для старой строки, которую никто не трогает, он и
    # есть время начала.
    moment = row.updated_at
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def draft_ref(peer_id: int, started: datetime) -> str:
    return f"draft:{peer_id}:{int(started.timestamp())}"


def _delivery_phrase(row: OrderDraftRow) -> str:
    """«в пункт выдачи Ozon (адрес)» вместо внутренней метки черновика."""
    label = row.delivery_label or ""
    address = label.split(": ", 1)[1] if ": " in label else ""
    if row.delivery_method == "ozon_pvz":
        phrase = "в пункт выдачи Ozon"
    elif row.delivery_method == "cdek_pvz":
        phrase = "в пункт выдачи СДЭК"
    elif row.delivery_method == "cdek_courier":
        phrase = "курьером СДЭК"
    else:
        return label or "выбранным способом"
    return f"{phrase} ({address})" if address else phrase


def nudge_text(row: OrderDraftRow) -> str:
    items = list(row.items or [])
    items_total = float(row.items_total or 0)
    if row.delivery_method and row.delivery_cost is not None:
        gap = None
        threshold = free_delivery_threshold()
        if threshold is not None and items_total < threshold:
            gap = round(threshold - items_total, 2)
            # «Добавьте ещё пачку» — только если одной пачки и хватит.
            cheapest = catalog_service.cheapest_in_stock_price()
            if cheapest is None or gap > cheapest:
                gap = None
        details = row.details or {}
        # У Ozon цена до выбора пункта предварительная: называем её «около».
        approximate = row.delivery_method == "ozon_pvz" and not details.get("ozon_point_id")
        total = items_total + float(row.delivery_cost)
        return templates.draft_nudge_priced(
            items,
            delivery_label=_delivery_phrase(row),
            total=total,
            threshold_gap=gap,
            approximate=approximate,
        )
    return templates.draft_nudge_unpriced(items, items_total=items_total)


def _storefront(row: OrderDraftRow) -> bool:
    return (row.details or {}).get("origin") == "storefront"


def _silence(row: OrderDraftRow) -> timedelta:
    if settings.storefront_early_nudge_enabled and _storefront(row):
        return timedelta(minutes=settings.storefront_draft_nudge_after_minutes)
    return timedelta(hours=settings.draft_nudge_after_hours)


def _point_missing(row: OrderDraftRow) -> bool:
    details = row.details or {}
    return (row.delivery_method in ("ozon_pvz", "cdek_pvz")
            and not (details.get("ozon_point_id") or details.get("delivery_point")))


async def _message(row: OrderDraftRow) -> tuple[str, dict | None]:
    """Текст и кнопки напоминания. Витринный заказ без пункта — вопрос о месте."""
    if settings.storefront_early_nudge_enabled and _storefront(row) and _point_missing(row):
        from app.modules.orders import geo

        with_geo = await geo.offer_for(row.peer_id, row.delivery_method)
        carrier = "Ozon" if row.delivery_method == "ozon_pvz" else "СДЭК"
        keyboard = keyboards.inline([[geo.button((row.details or {}).get("version"))]]) if with_geo else None
        return templates.draft_nudge_where(list(row.items or []), carrier=carrier, geo=with_geo), keyboard
    return nudge_text(row), nudge_keyboard(row)


def nudge_keyboard(row: OrderDraftRow) -> dict | None:
    """«Оформить» — только когда доставка посчитана: иначе оформлять нечего."""
    if not (row.delivery_method and row.delivery_cost is not None):
        return None
    return keyboards.inline([[keyboards.text_button(
        "Оформить", {"a": "checkout", "v": (row.details or {}).get("version")}, "positive"
    )]])


async def _conversation_state(session, peer_id: int, started: datetime):
    """Последняя реплика диалога и писал ли менеджер после начала черновика."""
    last = (
        await session.execute(
            select(ConversationMessage.role, ConversationMessage.created_at)
            .where(ConversationMessage.peer_id == peer_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(1)
        )
    ).first()
    manager_since = (
        await session.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(
                ConversationMessage.peer_id == peer_id,
                ConversationMessage.author == dialog_history.AUTHOR_MANAGER,
                ConversationMessage.created_at >= started,
            )
        )
    ).scalar_one()
    return last, manager_since > 0


async def check_drafts(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not marketing.in_window(now):
        return {"sent": 0, "skipped": "вне окна продающих сообщений"}

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {"sent": 0, "skipped": "нет базы"}

    max_age = timedelta(hours=settings.draft_nudge_max_age_hours)
    sent = 0

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(OrderDraftRow).where(
                    OrderDraftRow.stage.in_(_STAGES),
                    OrderDraftRow.updated_at >= now - max_age,
                )
            )
        ).scalars().all()

        candidates = []
        for row in rows:
            details = row.details or {}
            # Счёт уже выставляли: дальше заказ ведут напоминания об оплате.
            if details.get("order_id") or not row.items:
                continue
            started = _started_at(row)
            if now - started > max_age:
                continue
            last, manager_wrote = await _conversation_state(session, row.peer_id, started)
            if manager_wrote or last is None:
                continue
            role, last_at = last
            last_at = last_at if last_at.tzinfo else last_at.replace(tzinfo=timezone.utc)
            if role != "assistant" or now - last_at < _silence(row):
                continue
            candidates.append((row, started))

    for row, started in candidates:
        ref = draft_ref(row.peer_id, started)
        if await client_messages.already_sent(ref, templates.DRAFT_NUDGE):
            continue
        if await escalation_state.is_open(row.peer_id):
            continue
        if await marketing.is_opted_out(row.peer_id) or await marketing.is_unreachable(row.peer_id):
            continue
        text, keyboard = await _message(row)
        if await client_messages.send(
            peer_id=row.peer_id,
            ref=ref,
            event_type=templates.DRAFT_NUDGE,
            text=text,
            keyboard=keyboard,
        ):
            sent += 1
            logger.info(
                "Напомнили про брошенный черновик peer_id=%s (начат %s)",
                row.peer_id, worktime.to_msk(started).isoformat(timespec="minutes"),
            )

    return {"drafts": len(candidates), "sent": sent}
