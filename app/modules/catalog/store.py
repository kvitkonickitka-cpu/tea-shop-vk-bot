"""Ассортимент из базы (`CATALOG_SOURCE=db`): таблицы `shop.catalog` и `shop.bundle_items`.

Бот видит каталог в том же виде, что и из Google Таблицы, — словарями с
`name`, `price`, `in_stock`, `recommended` и остальным (`service.load_items`).
Поэтому всё, что работает по каталогу (промпт, кнопки «Взять», допродажа,
заварка во «вручено», сборка), не знает, откуда он пришёл. Сверх того у
товара из базы есть `sku_id`, `kind` и `net_weight_g`.

**Как быстро правка из Metabase доходит до бота.** Каждый контейнер раз в
5 секунд спрашивает номер последней записи журнала изменений — одну строку
по индексу — и перечитывает каталог, только если номер вырос. Журнал пишет
триггер на любую правку (`schema.py`), так что номер меняется с каждым
изменением, даже сделанным в обход форм. Выходит не дольше нескольких секунд
при обещанной минуте. `LISTEN/NOTIFY` дал бы мгновенность, но требует
отдельного соединения на контейнер, которое надо держать и переподключать
после сбоя базы, — ради разницы в секунды не стоит.

**Пустой каталог — пауза, а не откат.** Если в базе нет ни одного товара или
база не читается при холодном старте, `current_items()` пуст: бот не
продаёт, покупатель получает сообщение о паузе, в Ops — тревога
(`alert_unavailable`). Тихо вернуться к таблице или к `catalog.json` нельзя:
три тестовых сорта по 1 500 ₽ выглядели бы как настоящий ассортимент.
Прочитанный раньше каталог при временном сбое базы остаётся в памяти.
"""

from __future__ import annotations

import html
import logging
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.catalog import schema

logger = logging.getLogger(__name__)

_MEMORY_TTL_SECONDS = 5
# Тревога о пустом каталоге — не чаще раза в полчаса с контейнера: каждый
# покупатель в паузе иначе будил бы Ops заново.
_ALERT_COOLDOWN_SECONDS = 30 * 60

_memory: list[dict] = []
_version: int | None = None
_checked_at = 0.0
_alerted_at: float | None = None

_ITEMS_SQL = """
select sku_id, kind, name, description, net_weight_g, raw_material, gtin, price, on_sale,
       vk_market_url, recommend_with, brew_guide, video_url, is_new, synonyms, category
  from shop.catalog
 where active and price is not null
 order by on_sale desc, sort_order, name
"""
_PARTS_SQL = """
select b.bundle_sku, sum(b.quantity * c.net_weight_g) as grams
  from shop.bundle_items b join shop.catalog c on c.sku_id = b.item_sku
 where b.active
 group by b.bundle_sku
"""


def current_items() -> list[dict]:
    """Товары из базы — копии, чтобы вызывающий не испортил память."""
    return [dict(item) for item in _memory]


def unavailable() -> bool:
    """Продавать нечего: каталог пуст или ни разу не прочитан."""
    return not _memory


def _grams(value) -> str:
    number = float(value)
    return f"{number:g} г"


def build_items(rows: list[dict], set_weights: dict[str, float]) -> list[dict]:
    """Строки `shop.catalog` → товары в том виде, в каком их ждёт бот."""
    names = {row["sku_id"]: row["name"] for row in rows}
    items = []
    for row in rows:
        weight = row["net_weight_g"] if row["net_weight_g"] is not None else set_weights.get(row["sku_id"])
        items.append({
            "sku_id": row["sku_id"],
            # Как в коде сборки и чека: пачка (блин тоже) или сет / набор.
            "kind": "set" if row["kind"] in schema.BUNDLES else "pack",
            "name": row["name"],
            "price": int(row["price"]),
            "package_sizes": [_grams(weight)] if weight else [],
            # «В наличии» для покупателя — решение владельца (`on_sale`).
            # Блок склада добавит сюда «сырья хватает».
            "in_stock": bool(row["on_sale"]),
            "gtin": row["gtin"] or "",
            "link": row["vk_market_url"] or "",
            "description": row["description"] or "",
            # Названия, а не коды: так «С чем советуем» читает модель и код
            # допродажи. Ссылка на архивный артикул молча пропадает.
            "recommended": [names[sku] for sku in (row["recommend_with"] or []) if sku in names],
            "is_new": bool(row["is_new"]),
            "brewing": row["brew_guide"] or "",
            "video": row["video_url"] or "",
            "synonyms": list(row["synonyms"] or []),
            "net_weight_g": float(weight) if weight else None,
            "category": row["category"] or "",
        })
    return items


async def ensure_fresh(*, force: bool = False) -> None:
    """Подтянуть каталог из базы, если он изменился. Не бросает."""
    global _memory, _version, _checked_at
    if not force and _checked_at and time.monotonic() - _checked_at < _MEMORY_TTL_SECONDS:
        return
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            version = await session.scalar(text("select coalesce(max(id), 0) from shop.catalog_changes"))
            if not force and version == _version and _memory:
                _checked_at = time.monotonic()
                return
            rows = [dict(row) for row in (await session.execute(text(_ITEMS_SQL))).mappings()]
            set_weights = {
                row.bundle_sku: float(row.grams)
                for row in (await session.execute(text(_PARTS_SQL)))
                if row.grams is not None
            }
    except Exception:
        # Прочитанное раньше остаётся; не читали ни разу — каталог пуст, и это
        # пауза для покупателя, а не откат на таблицу.
        logger.warning("Каталог из базы не прочитали", exc_info=True)
        _checked_at = time.monotonic()
        return
    _memory = build_items(rows, set_weights)
    _version = version
    _checked_at = time.monotonic()


def reset() -> None:
    """Забыть прочитанное — для тестов и после переключения источника."""
    global _memory, _version, _checked_at, _alerted_at
    _memory = []
    _version = None
    _checked_at = 0.0
    _alerted_at = None


async def alert_unavailable() -> None:
    """🚨 в Ops: каталог пуст или не читается, покупатели получают паузу. Не бросает."""
    global _alerted_at
    now = time.monotonic()
    if _alerted_at is not None and now - _alerted_at < _ALERT_COOLDOWN_SECONDS:
        return
    _alerted_at = now
    if not settings.telegram_ops_chat_id:
        logger.error("Каталог в базе пуст или не читается — покупатели получают паузу")
        return
    from app.modules.dialog import telegram_client

    try:
        # Прямо в телеграм, мимо очереди: очередь живёт в той же базе,
        # которая, возможно, и не отвечает.
        await telegram_client.send_message(
            "🚨 <b>Каталог в базе пуст или не читается</b>\n"
            "Бот не продаёт: покупателям отвечает, что приём заказов на паузе. "
            "Проверьте shop.catalog (Metabase → «Служебное» → «Каталог») и подключение к базе.",
            chat_id=settings.telegram_ops_chat_id,
        )
    except Exception:
        logger.exception("Тревогу о пустом каталоге в Ops не отправили")


_CLAIM_SQL = """
update shop.catalog_changes set notified_at = now()
 where id in (select id from shop.catalog_changes where notified_at is null
               order by id limit 200 for update skip locked)
returning id, sku_id, entity, action, field, old_value, new_value, changed_by, note
"""


def _change_line(row, names: dict[str, str]) -> str | None:
    """Строка для Ops — только цена, продажа, архив и новый артикул.

    Остальные правки (описание, заварка, ссылки) видны в журнале на вкладке
    «Каталог»; писать о каждой — шум.
    """
    title = f"{html.escape(row.sku_id)} «{html.escape(names.get(row.sku_id, ''))}»"
    who = f" — {html.escape(row.changed_by)}" if row.changed_by else ""
    note = f" ({html.escape(row.note)})" if row.note else ""
    if row.entity != "артикул":
        return None
    if row.action == "добавлен":
        return f"➕ Добавлен артикул {title}{who}{note}"
    if row.field == "price":
        return f"💰 Цена изменена: {title} {row.old_value or '—'} → {row.new_value or '—'} ₽{who}{note}"
    if row.field == "on_sale":
        state = "включён в продажу" if row.new_value == "true" else "снят с продажи"
        return f"{'✅' if row.new_value == 'true' else '⏸'} {title} {state}{who}{note}"
    if row.field == "active":
        state = "в архиве" if row.new_value == "false" else "возвращён из архива"
        return f"🗄 {title} {state}{who}{note}"
    return None


async def notify_changes() -> dict:
    """Сообщить в Ops о ценах и продаже из журнала — тик расписания.

    Отметку ставит тот, кто забрал записи (`FOR UPDATE SKIP LOCKED`): два
    контейнера одну правку не объявят дважды. Записи без сообщения тоже
    отмечаются — иначе каждый тик перебирал бы их снова.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        if not await session.scalar(text("select to_regclass('shop.catalog_changes') is not null")):
            return {"skipped": "схемы shop нет"}
        rows = (await session.execute(text(_CLAIM_SQL))).all()
        names = dict((await session.execute(text("select sku_id, name from shop.catalog"))).all()) if rows else {}
        await session.commit()
    lines = [line for line in (_change_line(row, names) for row in sorted(rows, key=lambda r: r.id)) if line]
    if lines and settings.telegram_ops_chat_id:
        from app.messages import manager as manager_messages

        await manager_messages.notify(
            manager_messages.CATALOG_CHANGES,
            "<b>Каталог</b>\n" + "\n".join(lines[:30]) + ("\n…" if len(lines) > 30 else ""),
            chat_id=settings.telegram_ops_chat_id,
        )
    return {"записей": len(rows), "в Ops": len(lines)}
