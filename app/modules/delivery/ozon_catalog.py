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

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, select, update
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
    # Клиент описывает пункт словами, которых в адресе нет: «пвз на
    # Ставропольской», «постамат Озон». Оставь их в запросе — и они либо
    # ничего не найдут, либо наберут очков на чужом адресе.
    "пвз", "пункт", "пункты", "выдача", "выдачи", "постамат", "озон", "ozon",
}


def normalize(text: str) -> str:
    """Значимые слова адреса: без пунктуации и без «улица», «дом», «край»."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    words = [
        w
        for w in cleaned.split()
        # Одинокие буквы — всегда остаток сокращения: «пр-т» превращается в
        # «пр» и «т», и второе ничего не значит. Цифры в одиночку значат
        # (дом 5), их оставляем.
        if w and w not in _NOISE_WORDS and (len(w) > 1 or w.isdigit())
    ]
    return " ".join(words)


# Падежные окончания, от длинных к коротким: срезаем ровно одно, самое
# длинное подходящее. «-ов» и «-ев» сюда намеренно не входят — с ними
# «Ростов» превратился бы в «рост», а улица Кирова и так сходится с «Киров».
_ENDINGS = (
    "ого", "его", "ому", "ему", "ыми", "ими",
    "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ей", "ем", "ом",
    "ах", "ях", "ам", "ям", "ую", "юю", "ью", "ия", "ья", "ье",
    "а", "я", "о", "е", "у", "ю", "ы", "и", "ь", "й",
)


def stem(word: str) -> str:
    """Слово без падежного окончания.

    Клиент пишет «на Ставропольской», в каталоге стоит «Ставропольская» —
    по буквам это разные строки. Сравнение по основе сводит формы одного
    слова вместе.

    И оно же разводит то, что сводить нельзя. Город — «краснодар», а
    прилагательное края — «краснодарский» → «краснодарск»: основы разные, и
    пункт в Анапе больше не отвечает на запрос про Краснодар. Поиск по
    вхождению подстроки этого не различал — с него и начались Анапа с
    Армавиром в списке краснодарских пунктов.

    Остаток короче двух букв не режем, иначе от «Уфы» остаётся «у».
    """
    if not word or word[0].isdigit():
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[: -len(ending)]
    return word


def stems(text: str) -> list[str]:
    """Основы значимых слов строки."""
    return [stem(word) for word in normalize(text).split()]


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

        # Первую страницу берём до цикла: дальше каждая следующая едет
        # одновременно с адресами текущей.
        try:
            page, next_cursor = await ozon_client.delivery_point_ids(cursor=cursor)
        except Exception:
            logger.exception("Не получили страницу каталога Ozon")
            page, next_cursor = [], cursor

        while page:
            ids = [p.get("delivery_point_id") for p in page if p.get("delivery_point_id")]

            # Следующую страницу просим, не дожидаясь адресов текущей. Оба
            # запроса идут к Ozon примерно по полторы секунды, и заход, делая
            # их по очереди, половину времени просто ждал.
            ahead = (
                asyncio.create_task(ozon_client.delivery_point_ids(cursor=next_cursor))
                if next_cursor
                else None
            )

            try:
                details = await ozon_client.delivery_points_info(ids[:_INFO_BATCH]) if ids else []
            except Exception:
                logger.exception("Не получили адреса пунктов Ozon")
                if ahead is not None:
                    ahead.cancel()
                break

            if details:
                await _save_points(session, details, pass_number)
                seen += len(details)
                result["points"] += len(details)
            result["pages"] += 1

            cursor = next_cursor
            # Фиксируем после каждой страницы, а не в конце захода. У
            # контейнера 60 секунд на весь тик расписания, и когда отчёты с
            # проверкой заказов съедали остаток, выгрузку убивали на середине
            # — вместе со всем, что она успела, потому что коммит был один и
            # в самом конце. Каждый следующий тик начинал с того же места.
            state.cursor = cursor or None
            state.seen = seen
            await session.commit()

            if not cursor:
                # Дошли до конца: следующий проход начнём сначала, чтобы
                # подхватить новые и закрывшиеся пункты.
                #
                # И только здесь можно гасить пропавшие. Ozon не сообщает,
                # что пункт исчез, — он просто перестаёт его отдавать, и
                # заметить это можно единственным способом: сверить, кто
                # встретился за полный проход. Прерванный проход для этого не
                # годится — погасили бы всё, до чего не дошли.
                #
                # Пустой проход тоже не годится: если Ozon разом отдаст пустой
                # каталог, мы погасим вообще всё и останемся без пунктов.
                if seen:
                    result["gone"] = await _deactivate_missing(session, pass_number)
                    state.pass_number = pass_number + 1
                else:
                    logger.warning("Каталог Ozon пуст за весь проход — ничего не гасим")
                state.completed_at = datetime.now(timezone.utc)
                state.seen = 0
                seen = 0
                await session.commit()
                result["finished"] = True
                break

            if time.monotonic() - started >= budget_seconds:
                if ahead is not None:
                    ahead.cancel()
                break

            try:
                page, next_cursor = await ahead
            except Exception:
                logger.exception("Не получили страницу каталога Ozon")
                break

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


@dataclass(frozen=True)
class Found:
    """Что нашлось по названному клиентом городу и адресу.

    `hint_matched` — сошёлся ли адрес. Без этого признака нельзя различить
    «вот пункты на нужной улице» и «улицу не нашли, вот что есть в городе»,
    а клиенту это две разные новости.
    """

    points: list
    total: int
    hint_matched: bool


# Сколько строк города вытаскиваем из базы на отбор. Точный отбор по
# основам идёт уже у нас, в SQL — фильтр по целому слову (см. `_word_regex`).
_CANDIDATE_LIMIT = 1000


def _word_regex(base: str) -> str:
    """Регулярка Postgres: целое слово с основой `base` и любым окончанием.

    `stem()` срезает ровно одно окончание из `_ENDINGS`, значит любое слово
    с этой основой — это основа плюс одно из них. Регулярка поэтому шире
    точного отбора и ничего не теряет, а от подстроки отличается главным:
    «краснодарский» под «краснодар» больше не подходит.

    Раньше здесь был `LIKE '%краснодар%'`, и на Краснодар из базы вставала
    вся краевая выборка — Анапа, Сочи, Армавир — первыми 1000 строками без
    порядка. Отбор по городу шёл уже после потолка, и до него доживала лишь
    часть краснодарских пунктов: 26.09.2026 бот нашёл на Ставропольской два
    пункта там, где их около десяти.
    """
    endings = "|".join(re.escape(ending) for ending in _ENDINGS)
    return f"(^| ){re.escape(base)}({endings})?( |$)"


def _candidates(city_stems: list[str], hint_stems: list[str]):
    """Выборка по городу: строки, где есть все слова города целиком.

    Закрытые пункты не показываем никогда — это дорога к запертой двери.
    Сначала строки, где больше слов названного адреса: в городе размером с
    Москву пунктов больше потолка, и названная улица не должна зависеть от
    того, какие строки база отдала первыми.
    """
    statement = select(OzonDeliveryPoint).where(OzonDeliveryPoint.is_active.isnot(False))
    for part in city_stems[:3]:
        statement = statement.where(OzonDeliveryPoint.search_text.op("~")(_word_regex(part)))
    if hint_stems:
        hits = sum(
            case((OzonDeliveryPoint.search_text.op("~")(_word_regex(part)), 1), else_=0)
            for part in hint_stems[:6]
        )
        statement = statement.order_by(hits.desc(), OzonDeliveryPoint.id)
    else:
        statement = statement.order_by(OzonDeliveryPoint.id)
    return statement.limit(_CANDIDATE_LIMIT)


def _hint_score(row_words: list[str], row_stems: list[str], hint: list[str]) -> int:
    """Насколько адрес пункта похож на то, что назвал клиент.

    Считаем совпадения, а не требуем их все. Клиент пишет «пвз на
    Ставропольской 230», и слова «пвз» в адресе каталога нет — при поиске «по
    всем словам сразу» такой запрос не находил ничего, хотя нужный пункт
    лежал в базе. Слово из адреса даёт очко, точное совпадение — два.
    """
    score = 0
    for word in hint:
        base = stem(word)
        if word in row_words:
            score += 2
        elif base and base in row_stems:
            score += 1
    return score


async def search(city: str, hint: str = "", limit: int = 5) -> Found:
    """Пункты под названный город и адрес.

    Город обязателен: совпадать должны все его слова, и совпадать основами,
    иначе в списке краснодарских пунктов оказывается Анапа. Адрес — дело
    вкуса клиента, поэтому он только выстраивает порядок. Если адрес не
    сошёлся ни с одним пунктом, отдаём город целиком и честно говорим об
    этом признаком `hint_matched`.
    """
    city_words = normalize(city).split()
    city_stems = [stem(word) for word in city_words]
    if not city_stems:
        return Found([], 0, False)

    hint_words = [word for word in normalize(hint).split() if word not in city_words]

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return Found([], 0, False)

    async with session_factory() as session:
        rows = (
            await session.execute(
                _candidates(city_stems, [stem(word) for word in hint_words if stem(word)])
            )
        ).scalars().all()

    scored: list[tuple[int, int, OzonDeliveryPoint]] = []
    for row in rows:
        row_words = (row.search_text or "").split()
        row_stems = [stem(word) for word in row_words]
        # Город — условие, а не пожелание: пункт в Армавире клиенту из
        # Краснодара не годится, как бы похоже ни звучал адрес.
        if any(part not in row_stems for part in city_stems):
            continue
        scored.append((_hint_score(row_words, row_stems, hint_words), len(row_words), row))

    if not scored:
        return Found([], 0, False)

    best = max(item[0] for item in scored)
    hint_matched = bool(hint_words) and best > 0
    # Когда адрес сошёлся, показываем только лучшие совпадения. Клиент,
    # назвавший «Ставропольская 230», должен получить дом 230, а не всю улицу:
    # один пункт в ответе бот закрепляет за заказом сам, а из списка просит
    # выбрать — то есть лишний круг разговора на ровном месте.
    chosen = [item for item in scored if item[0] == best] if hint_matched else scored

    # Сначала самые похожие, а среди равных — те, где адрес короче: длинные
    # адреса у Ozon это обычно приписки про этаж и вход.
    chosen.sort(key=lambda item: (-item[0], item[1], item[2].id))
    return Found([item[2] for item in chosen[:limit]], len(chosen), hint_matched)


_KIND_LABELS = {"pvz": "пункты выдачи", "postamat": "постаматы", "unknown": "тип не определён"}


async def stats() -> dict:
    """Что лежит в каталоге: сколько пунктов, каких и сколько из них закрытых.

    Отдельно от `sync`, чтобы посмотреть можно было не запуская выгрузку.
    """
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {"error": "база недоступна"}

    from sqlalchemy import func as sql_func

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    OzonDeliveryPoint.kind,
                    OzonDeliveryPoint.is_active,
                    sql_func.count(),
                ).group_by(OzonDeliveryPoint.kind, OzonDeliveryPoint.is_active)
            )
        ).all()
        state = await session.get(OzonSyncState, 1)

    result: dict = {"всего": 0, "из них закрытых": 0}
    for kind, is_active, amount in rows:
        # Колонка с типом появилась позже самой выгрузки, так что у строк,
        # записанных до неё, тип пустой. Отдельной графой — по ней видно,
        # какая часть каталога ещё не обновлялась с тех пор.
        label = _KIND_LABELS.get(kind or "", "выгружены до появления типа")
        result[label] = result.get(label, 0) + amount
        result["всего"] += amount
        if is_active is False:
            result["из них закрытых"] += amount

    result["проход"] = (state.pass_number if state else 1) or 1
    result["полный обход завершался"] = (
        state.completed_at.strftime("%d.%m.%Y %H:%M")
        if state and state.completed_at
        else "ни разу"
    )
    return result


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
