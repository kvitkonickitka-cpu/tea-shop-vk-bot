"""Клиенты и тестовые данные для аналитики.

Тестовые аккаунты (`TEST_VK_IDS`) — владельцы и проверочные страницы: их
заказы и сами они помечаются `is_test` и в представления для DataLens не
попадают. Заказы, оплаченные раньше `TEST_PAID_BEFORE` (тестовый магазин
ЮKassa), — тоже. Пометку ставит тик расписания (`sync`): новые строки
появляются постоянно, и проверять их каждой вставкой по всему коду
значило бы трогать десяток мест ради одного флага.

Тестовые заказы не предлагаются «как в прошлый раз» и не дают повторных
касаний реальным клиентам (`test_order_filter`). Тестовым аккаунтам —
предлагаются: владелец должен иметь возможность проверить всё на себе.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.client_key import client_key
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.analytics.models import Client
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

_test_ids: set[int] | None = None
_test_ids_raw: str | None = None


async def test_peer_ids() -> set[int]:
    """VK ID тестовых аккаунтов. Короткие имена и ссылки переводятся один раз."""
    global _test_ids, _test_ids_raw
    raw = settings.test_vk_ids or ""
    if _test_ids is not None and _test_ids_raw == raw:
        return _test_ids
    from app.modules.dialog import vk_client

    found: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        if not part.strip():
            continue
        resolved = await vk_client.resolve_user_id(part.strip())
        if resolved is None:
            logger.error("TEST_VK_IDS: «%s» не удалось перевести в VK ID", part.strip())
            continue
        found.add(resolved)
    _test_ids, _test_ids_raw = found, raw
    return found


def test_order_filter(test_ids: set[int]):
    """Условие на заказы: тестовые — только тестовым аккаунтам."""
    if not test_ids:
        return Order.is_test.is_(False)
    return or_(Order.is_test.is_(False), Order.peer_id.in_(test_ids))


async def ensure_client(peer_id: int, *, ref: str | None = None, ref_source: str | None = None) -> None:
    """Завести клиента при первом контакте. Повторный вызов ничего не меняет.

    Метка кампании пишется только сейчас: ref из второго сообщения — это
    уже не то, откуда человек пришёл.
    """
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    is_test = peer_id in await test_peer_ids()
    statement = insert(Client).values(
        peer_id=peer_id, client_key=client_key(peer_id),
        ref=(ref or None) and str(ref)[:200], ref_source=(ref_source or None) and str(ref_source)[:200],
        is_test=is_test,
    ).on_conflict_do_nothing(index_elements=[Client.peer_id])
    try:
        async with session_factory() as session:
            await session.execute(statement)
            await session.commit()
    except Exception:
        logger.exception("Не завели клиента peer_id=%s", peer_id)


async def sync(*, apply: bool = True) -> dict:
    """Досоздать клиентов, посчитать ключи, пометить тестовые данные.

    `apply=False` — только показать, что будет помечено.
    """
    test_ids = sorted(await test_peer_ids())
    paid_before = None
    if settings.test_paid_before:
        paid_before = datetime.fromisoformat(settings.test_paid_before)
    async with get_session_factory()() as session:
        rules = []
        if test_ids:
            rules.append(Order.peer_id.in_(test_ids))
        if paid_before:
            rules.append(Order.paid_at < paid_before)
        orders_to_mark = (await session.execute(
            select(Order.id).where(Order.is_test.is_(False), or_(*rules)).order_by(Order.id)
        )).scalars().all() if rules else []
        clients_to_mark = (await session.execute(
            select(Client.peer_id).where(Client.is_test.is_(False), Client.peer_id.in_(test_ids))
        )).scalars().all() if test_ids else []
        if not apply:
            return {"заказов пометим тестовыми": len(orders_to_mark), "номера": list(orders_to_mark),
                    "клиентов пометим тестовыми": len(clients_to_mark)}

        # Клиенты, которых ещё нет: первый контакт — самое раннее из диалога и заказов.
        await session.execute(text(
            "insert into clients (peer_id, first_contact_at, is_test)"
            " select peer_id, min(at), false from ("
            "  select peer_id, started_at as at from conversations"
            "  union all select peer_id, created_at from orders) seen"
            " group by peer_id on conflict (peer_id) do nothing"
        ))
        if orders_to_mark:
            await session.execute(text("update orders set is_test = true where id = any(:ids)"),
                                  {"ids": list(orders_to_mark)})
        if test_ids:
            await session.execute(text("update clients set is_test = true where peer_id = any(:ids)"),
                                  {"ids": test_ids})
        missing = (await session.execute(
            select(Client.peer_id).where(Client.client_key.is_(None))
        )).scalars().all()
        for peer_id in missing:
            key = client_key(peer_id)
            if key:
                await session.execute(text("update clients set client_key = :k where peer_id = :p"),
                                      {"k": key, "p": peer_id})
        await session.commit()
    return {"помечено заказов": len(orders_to_mark), "помечено клиентов": len(clients_to_mark),
            "ключей посчитано": sum(1 for p in missing if client_key(p))}
