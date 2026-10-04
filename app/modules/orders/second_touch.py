"""Второй шанс: другой сорт после проигнорированного «Повторить» или «Не моё».

Срабатывает по вручённому заказу в двух случаях:

- «Повторить заказ?» ушло, а клиент через `second_touch_after_days` так и не
  ответил, не нажал кнопок и не заказал — повторять он не хочет, но,
  может быть, захочет попробовать другое;
- заказ оценён «Не моё» — «Повторить» не уходит вовсе, а второй шанс
  приходит в тот же срок, когда пришло бы оно.

Сорт выбирает код, а не модель: из столбца «С чем советуем» купленного, в
наличии, клиент его не покупал и не оценивал «Не моё»; новинки первыми.
Подходящего нет — касание не уходит: придумывать допродажу нельзя.
Одно на цикл — ключ журнала из номера заказа.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import keyboard as keyboards, templates
from app.messages.models import ClientNotice, FunnelEvent
from app.modules.dialog.models import ConversationMessage
from app.modules.orders import feedback, repeat_nudge, repository as orders_repository, retention, take
from app.modules.orders.models import Order, OrderRating

logger = logging.getLogger(__name__)


def _key(name: str) -> str:
    return (name or "").strip().casefold()


async def purchases(peer_id: int) -> tuple[list[str], set[str]]:
    """Что клиент покупал (свежее — первым) и что из этого оценил «Не моё»."""
    async with get_session_factory()() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.peer_id == peer_id,
                    Order.payment_status == orders_repository.PAID,
                    Order.status != orders_repository.CANCELED,
                ).order_by(Order.created_at.desc())
            )
        ).scalars().all()
        disliked_orders = set((await session.execute(
            select(OrderRating.order_id).where(OrderRating.peer_id == peer_id, OrderRating.rating == "no")
        )).scalars().all())
    bought: list[str] = []
    disliked: set[str] = set()
    for order in orders:
        for item in order.items or []:
            name = item.get("name") or ""
            if name and name not in bought:
                bought.append(name)
            if order.id in disliked_orders:
                disliked.add(_key(name))
    return bought, disliked


def pick_sorts(catalog: list[dict], bought: list[str], disliked: set[str], limit: int = 1,
               *, novelties_only: bool = False) -> list[tuple[dict, str]]:
    """Сорта для предложения и купленный сорт, к которому их советуют.

    Только из «С чем советуем» купленного (кроме `novelties_only` —
    реактивация: новинки, которые клиент не брал). В наличии, не купленные
    и не оценённые «Не моё»; новинки — первыми.
    """
    by_name = {_key(item["name"]): item for item in catalog}
    seen = {_key(name) for name in bought} | disliked
    found: list[tuple[dict, str]] = []
    if novelties_only:
        for item in catalog:
            if item.get("is_new") and item.get("in_stock", True) and _key(item["name"]) not in seen:
                found.append((item, ""))
                seen.add(_key(item["name"]))
    else:
        for name in bought:
            source = by_name.get(_key(name))
            for advised in (source or {}).get("recommended") or []:
                item = by_name.get(_key(advised))
                if item is None or not item.get("in_stock", True) or _key(item["name"]) in seen:
                    continue
                found.append((item, take.display_name(source["name"])))
                seen.add(_key(item["name"]))
        found.sort(key=lambda pair: not pair[0].get("is_new"))
    return found[:limit]


def short_description(item: dict, limit: int = 120) -> str:
    """Первое предложение описания из таблицы — коротко."""
    text = " ".join((item.get("description") or "").split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".")
    if len(first) > limit:
        first = first[: limit - 1].rsplit(" ", 1)[0] + "…"
    return first


def take_button(item: dict, kind: str) -> dict:
    return keyboards.text_button(
        take.label("Взять", item), {"a": "take", "n": item["name"], "t": kind}, "positive"
    )


def repeat_button(order_id: int, kind: str) -> dict:
    return keyboards.text_button("Повторить прошлый заказ", {"a": "repeat", "o": order_id, "t": kind})


async def _ignored(session, order: Order, since: datetime) -> bool:
    """Клиент после «Повторить» молчал: ни слова, ни нажатия, ни заказа."""
    said = (await session.execute(
        select(ConversationMessage.id).where(
            ConversationMessage.peer_id == order.peer_id,
            ConversationMessage.role == "user",
            ConversationMessage.created_at > since,
        ).limit(1)
    )).first()
    pressed = (await session.execute(
        select(FunnelEvent.id).where(
            FunnelEvent.peer_id == order.peer_id,
            FunnelEvent.event.like("button:%"),
            FunnelEvent.created_at > since,
        ).limit(1)
    )).first()
    return said is None and pressed is None


async def candidates(now: datetime) -> list[retention.Touch]:
    after = timedelta(days=settings.second_touch_after_days)
    shelf = timedelta(days=settings.second_touch_shelf_days)
    longest = timedelta(days=max(settings.repeat_nudge_max_days, settings.repeat_nudge_personal_max_days))
    touches = []
    async with get_session_factory()() as session:
        orders = await repeat_nudge.delivered_orders(session, now, longest + after + shelf)
        for order in orders:
            if await retention.already(order.id, templates.SECOND_TOUCH):
                continue
            if await repeat_nudge._bought_since(session, order):
                continue
            rating = await feedback.rating_of(order.id)
            if rating == "no":
                # Вместо «Повторить» — в его срок.
                due = await repeat_nudge.due_at(session, order)
            else:
                nudge = await session.get(ClientNotice, ("order:%d" % order.id, templates.REPEAT_NUDGE))
                if nudge is None or nudge.sent_at is None:
                    continue
                sent = retention.aware(nudge.sent_at)
                due = sent + after
                if now >= due and not await _ignored(session, order, sent):
                    continue
            if not retention.ripe(due, due + shelf, now):
                continue
            touches.append(retention.Touch(
                kind=templates.SECOND_TOUCH, peer_id=order.peer_id, order=order,
                due=due, expires=due + shelf, build=_builder(order, rating),
            ))
    return touches


def _builder(order: Order, rating: str | None):
    async def build() -> retention.Message | None:
        from app.modules.catalog import service as catalog_service

        bought, disliked = await purchases(order.peer_id)
        picked = pick_sorts(catalog_service.load_items(), bought, disliked, limit=1)
        if not picked:
            logger.info("Заказ %s: для второго шанса нет подходящего сорта", order.id)
            return None
        item, source = picked[0]
        text = templates.second_touch(
            offer=take.display_name(item["name"]), price=item["price"],
            description=short_description(item), source=source, rating=rating,
        )
        rows = [[take_button(item, templates.SECOND_TOUCH)]]
        if rating != "no" and settings.repeat_one_tap_enabled:
            rows.append([repeat_button(order.id, templates.SECOND_TOUCH)])
        return retention.Message(text, keyboards.inline(rows))
    return build
