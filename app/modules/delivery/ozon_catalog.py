"""Копия каталога пунктов выдачи Ozon и поиск по ней.

У Ozon нет поиска пункта ни по городу, ни по адресу: `delivery-point/list`
отдаёт голые идентификаторы постранично (не больше ста за раз), а адреса
приходится добирать методом `info`. Спрашивать это по ходу диалога нельзя —
каталог на всю страну, — поэтому держим копию у себя и ищем по ней.

Выгрузка идёт по таймеру и кусками: за один заход тратим ограниченное время
и запоминаем курсор, следующий тик продолжает с того же места. Контейнеру на
всё про всё отведено 60 секунд, и упереться в них посреди страницы нельзя.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.modules.delivery import ozon_client
from app.modules.delivery.models import OzonDeliveryPoint, OzonSyncState

logger = logging.getLogger(__name__)

# Сколько секунд за один заход. Меньше отведённых контейнеру 60 с запасом:
# после выгрузки в том же тике ещё работают отчёты и проверка заказов.
_BUDGET_SECONDS = 20
# Адреса добираем пачками: за раз Ozon отдаёт не больше ста.
_INFO_BATCH = 100

_NOISE_WORDS = {
    "россия", "область", "обл", "край", "республика", "район", "рн",
    "город", "г", "улица", "ул", "дом", "д", "проспект", "пр", "пр-т",
    "переулок", "пер", "шоссе", "ш", "бульвар", "б-р", "проезд",
    "микрорайон", "мкр", "корпус", "корп", "к", "строение", "стр",
    "округ", "внутригородской",
}


def normalize(text: str) -> str:
    """Значимые слова адреса: без пунктуации и без «улица», «дом», «край»."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    words = [w for w in cleaned.split() if w and w not in _NOISE_WORDS]
    return " ".join(words)


async def _load_state(session) -> OzonSyncState:
    state = await session.get(OzonSyncState, 1)
    if state is None:
        state = OzonSyncState(id=1, cursor=None, seen=0)
        session.add(state)
        await session.flush()
    return state


async def _save_points(
    session, points: list[ozon_client.DeliveryPoint], pass_number: int
) -> None:
    if not points:
        return
    rows = [
        {
            "id": point.id,
            "name": point.name,
            "address": point.address,
            "search_text": normalize(point.address),
            "is_active": point.is_active,
            "kind": point.kind,
            "seen_pass": pass_number,
            "updated_at": datetime.now(timezone.utc),
        }
        for point in points
        if point.id
    ]
    # Пункты приходят одни и те же на каждом проходе, поэтому обновляем на
    # месте, а не пытаемся вставить заново.
    statement = insert(OzonDeliveryPoint).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[OzonDeliveryPoint.id],
            set_={
                "name": statement.excluded.name,
                "address": statement.excluded.address,
                "search_text": statement.excluded.search_text,
                "is_active": statement.excluded.is_active,
                "kind": statement.excluded.kind,
                "seen_pass": statement.excluded.seen_pass,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


async def sync(budget_seconds: int = _BUDGET_SECONDS) -> dict:
    """Дотянуть каталог, сколько успеем за отведённое время."""
    # Кроме «сколько сделали за заход», отвечаем, где находится обход целиком.
    # Без этого по числу пунктов в базе не понять главного: каталог ещё не
    # докачался или он весь такой. Растущий номер прохода и заполненная дата
    # последнего полного обхода означают второе.
    result = {
        "pages": 0,
        "points": 0,
        "finished": False,
        "gone": 0,
        "проход": 0,
        "полный обход завершался": None,
        "осталось с прошлого захода": False,
        "skipped": None,
    }

    if not ozon_client.is_configured():
        result["skipped"] = "ключи Ozon не заданы"
        return result

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        result["skipped"] = "база недоступна"
        return result

    started = time.monotonic()

    async with session_factory() as session:
        state = await _load_state(session)
        cursor = state.cursor or ""
        seen = state.seen if cursor else 0
        pass_number = state.pass_number or 1

        # Цикл с проверкой в конце, а не в начале: иначе заход, у которого
        # бюджет съели подготовка или медленная база, не сделает ни одной
        # страницы — и выгрузка не сдвинется никогда.
        while True:
            try:
                page, next_cursor = await ozon_client.delivery_point_ids(cursor=cursor)
            except Exception:
                logger.exception("Не получили страницу каталога Ozon")
                break

            ids = [p.get("delivery_point_id") for p in page if p.get("delivery_point_id")]
            if ids:
                try:
                    details = await ozon_client.delivery_points_info(ids[:_INFO_BATCH])
                except Exception:
                    logger.exception("Не получили адреса пунктов Ozon")
                    break
                await _save_points(session, details, pass_number)
                seen += len(details)
                result["points"] += len(details)

            result["pages"] += 1
            cursor = next_cursor

            if not next_cursor or not page:
                # Дошли до конца: следующий проход начнём сначала, чтобы
                # подхватить новые и закрывшиеся пункты.
                #
                # И только здесь можно гасить пропавшие. Ozon не сообщает,
                # что пункт исчез, — он просто перестаёт его отдавать, и
                # заметить это можно единственным способом: сверить, кто
                # встретился за полный проход. Прерванный проход для этого не
                # годится — погасили бы всё, до чего не дошли.
                result["gone"] = await _deactivate_missing(session, pass_number)
                state.completed_at = datetime.now(timezone.utc)
                state.pass_number = pass_number + 1
                cursor = ""
                seen = 0
                result["finished"] = True
                break

            if time.monotonic() - started >= budget_seconds:
                break

        state.cursor = cursor or None
        state.seen = seen
        result["проход"] = state.pass_number or 1
        result["полный обход завершался"] = (
            state.completed_at.strftime("%d.%m.%Y %H:%M") if state.completed_at else "ни разу"
        )
        result["осталось с прошлого захода"] = bool(cursor)
        await session.commit()

    return result


async def _deactivate_missing(session, pass_number: int) -> int:
    """Погасить пункты, которых не было в только что завершённом проходе.

    Не удаляем: пункт может открыться снова, а в старых заказах он останется
    упомянут. Гашение достаточно — закрытые мы клиенту и так не показываем.
    """
    result = await session.execute(
        update(OzonDeliveryPoint)
        .where(
            OzonDeliveryPoint.seen_pass.isnot(None),
            OzonDeliveryPoint.seen_pass != pass_number,
            OzonDeliveryPoint.is_active.isnot(False),
        )
        .values(is_active=False, updated_at=datetime.now(timezone.utc))
    )
    gone = result.rowcount or 0
    if gone:
        logger.warning("Ozon перестал отдавать пункты, гасим: %s", gone)
    return gone


def _matching(query: str):
    """Запрос по значимым словам адреса, или None, если искать нечего.

    Клиент пишет адрес как придётся, поэтому сравниваем по словам: берём
    строки, где встречаются все слова запроса. Закрытые пункты не показываем
    никогда — это дорога к запертой двери.
    """
    words = normalize(query).split()
    if not words:
        return None

    statement = select(OzonDeliveryPoint).where(OzonDeliveryPoint.is_active.isnot(False))
    for word in words[:5]:
        statement = statement.where(OzonDeliveryPoint.search_text.contains(word))
    return statement


async def find(query: str, limit: int = 5) -> list[OzonDeliveryPoint]:
    """Пункты, подходящие под то, что назвал клиент."""
    statement = _matching(query)
    if statement is None:
        return []

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return []

    async with session_factory() as session:
        rows = (await session.execute(statement.limit(limit))).scalars().all()
    return list(rows)


async def count_matching(query: str) -> int:
    """Сколько всего пунктов подходит под запрос.

    Клиенту важно знать, из скольких он выбирает: пять адресов из сорока —
    это не выбор, а случайная выборка, и предлагать её как весь список
    нечестно.
    """
    statement = _matching(query)
    if statement is None:
        return 0

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return 0

    from sqlalchemy import func as sql_func

    async with session_factory() as session:
        total = await session.scalar(
            select(sql_func.count()).select_from(statement.subquery())
        )
    return int(total or 0)


async def count() -> int:
    """Сколько пунктов уже выгружено — для диагностики."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return 0
    async with session_factory() as session:
        from sqlalchemy import func as sql_func

        total = await session.scalar(sql_func.count(OzonDeliveryPoint.id))
    return int(total or 0)
