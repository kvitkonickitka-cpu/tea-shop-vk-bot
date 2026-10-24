"""Резерв сырья при оплате, снятие, смена партии и списание при сборке.

Порядок жизни пачки:

1. **Оплата** (`reserve_order`) — на каждую пачку позиции (у сета и набора —
   на каждое вложение) резерв сырья из партии с самым ранним «годен до», потом
   с самым ранним приходом. Партия, из которой не получится пачка со сроком не
   меньше `MIN_SHELF_LIFE_DAYS`, не берётся. Не хватило свободного сырья —
   резерв без партии, заказ всё равно идёт дальше, в Ops тревога.
2. **Отмена или возврат денег до сборки** (`release_order`) — резерв снят.
3. **Сборка** (`write_off`) — списание из зарезервированной партии; сборщик
   может взять другую (`change_lot`), тогда резерв переносится. Фактический
   вес больше номинала — разница идёт в потери фасовки.
4. **Возврат после вручения** (`note_return`) — пачка в граммы не
   возвращается; запись «возврат» в журнале, что с ней делать — решает владелец.

Всё под одной рекомендательной блокировкой: заказов мало, а два резерва из
одной партии одновременно не должны «увидеть» один и тот же свободный остаток.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.core import worktime
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.catalog import schema as catalog_schema

logger = logging.getLogger(__name__)

# Ключ блокировки склада: hashtext('shop.stock') посчитан один раз, чтобы не
# зависеть от функции на каждом вызове.
_LOCK = "select pg_advisory_xact_lock(hashtext('shop.stock'))"


def enabled() -> bool:
    """Склад по партиям работает вместе с каталогом в базе (`CATALOG_SOURCE=db`).

    Пока бот продаёт по Google Таблице, артикулов и состава наборов у заказа
    нет — резервировать нечего, а сборку останавливать нельзя.
    """
    from app.modules.catalog import service as catalog_service

    return catalog_service.from_db()


class StockError(Exception):
    """Сборка с этим сырьём невозможна. Текст — для сборщика."""


@dataclass
class Slot:
    """Место резерва: пачка позиции или вложение набора."""

    item_index: int
    unit_no: int
    part_no: int
    sku_id: str
    raw_material: str
    qty: Decimal
    unit: str
    shelf_life_months: int | None
    name: str


@dataclass
class ReserveResult:
    reserved: int = 0
    shortages: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _today() -> date:
    return worktime.to_msk(datetime.now(timezone.utc)).date()


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    # Последний день месяца, если в нём нет такого числа (31 января + 1 месяц).
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)


def pack_best_before(packed_on: date, shelf_life_months: int | None, lot_best_before: date | None) -> date | None:
    """Срок годности пачки: меньшее из «дата упаковки + наш срок» и «годен до» партии."""
    candidates = [d for d in (
        _add_months(packed_on, shelf_life_months) if shelf_life_months else None,
        lot_best_before,
    ) if d is not None]
    return min(candidates) if candidates else None


def shelf_ok(packed_on: date, shelf_life_months: int | None, lot_best_before: date | None) -> bool:
    """Хватит ли пачке срока: не меньше `MIN_SHELF_LIFE_DAYS`. Срок неизвестен — не запрещаем."""
    until = pack_best_before(packed_on, shelf_life_months, lot_best_before)
    return until is None or (until - packed_on).days >= settings.min_shelf_life_days


async def _catalog(session) -> tuple[dict[str, dict], dict[str, dict], dict[str, list[tuple[str, int]]]]:
    rows = [dict(r) for r in (await session.execute(text(
        "select sku_id, kind, name, net_weight_g, raw_material, shelf_life_months from shop.catalog where active"
    ))).mappings()]
    by_sku = {r["sku_id"]: r for r in rows}
    by_name = {r["name"].strip().casefold(): r for r in rows}
    parts: dict[str, list[tuple[str, int]]] = {}
    for row in (await session.execute(text(
        "select bundle_sku, item_sku, quantity from shop.bundle_items where active order by item_sku"
    ))):
        parts.setdefault(row.bundle_sku, []).append((row.item_sku, int(row.quantity)))
    return by_sku, by_name, parts


def _need(article: dict) -> tuple[Decimal, str]:
    """Сколько сырья на одну пачку: блин — штука, пачка — её граммы."""
    if article["kind"] == "блин":
        return Decimal(1), "шт"
    return Decimal(article["net_weight_g"] or 0), "г"


def slots_for(items: list[dict], by_sku: dict, by_name: dict, parts: dict) -> tuple[list[Slot], list[str]]:
    """Позиции заказа → места резерва. Второе — позиции, которых нет в каталоге."""
    slots, unknown = [], []
    for index, item in enumerate(items or []):
        article = by_sku.get(str(item.get("sku_id") or "")) or by_name.get(str(item.get("name", "")).strip().casefold())
        if article is None:
            unknown.append(str(item.get("name", "?")))
            continue
        quantity = int(item.get("quantity") or 1)
        if article["kind"] in catalog_schema.BUNDLES:
            components = []
            for sku, count in parts.get(article["sku_id"], []):
                components.extend([by_sku[sku]] * count if sku in by_sku else [])
            if not components:
                unknown.append(f"{article['name']} (состав не задан)")
                continue
        else:
            components = [article]
        for unit_no in range(quantity):
            for part_no, component in enumerate(components):
                qty, unit = _need(component)
                slots.append(Slot(index, unit_no, part_no, component["sku_id"], component["raw_material"],
                                  qty, unit, component.get("shelf_life_months"), component["name"]))
    return slots, unknown


async def _free_lots(session, raw_material: str) -> list[dict]:
    """Партии сырья по порядку выбора: ранний «годен до», потом ранний приход."""
    return [dict(r) for r in (await session.execute(text("""
        select lot_id, lot_code, unit, free, best_before from shop.v_lot_balance
        where raw_material = :raw and active and free > 0
        order by best_before nulls last, arrival_date, lot_id"""), {"raw": raw_material})).mappings()]


def _pick(lots: list[dict], slot: Slot, today: date, taken: dict[int, Decimal]) -> dict | None:
    for lot in lots:
        if lot["unit"] != slot.unit or not shelf_ok(today, slot.shelf_life_months, lot["best_before"]):
            continue
        if Decimal(lot["free"]) - taken.get(lot["lot_id"], Decimal(0)) >= slot.qty:
            return lot
    return None


async def _order_items(session, order_id: int) -> list[dict]:
    items = await session.scalar(text("select items from orders where id = :id"), {"id": order_id})
    return list(items or [])


async def reserve_order(order_id: int) -> ReserveResult:
    """Резерв под оплаченный заказ. Повторный вызов ничего не добавляет."""
    result = ReserveResult()
    if not enabled():
        return result
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text(_LOCK))
        items = await _order_items(session, order_id)
        by_sku, by_name, parts = await _catalog(session)
        slots, result.unknown = slots_for(items, by_sku, by_name, parts)
        existing = {
            (r.item_index, r.unit_no, r.part_no)
            for r in await session.execute(text(
                "select item_index, unit_no, part_no from shop.reservations where order_id = :o"), {"o": order_id})
        }
        today = _today()
        lots_cache: dict[str, list[dict]] = {}
        taken: dict[int, Decimal] = {}
        for slot in slots:
            if (slot.item_index, slot.unit_no, slot.part_no) in existing:
                continue
            if slot.raw_material not in lots_cache:
                lots_cache[slot.raw_material] = await _free_lots(session, slot.raw_material)
            lot = _pick(lots_cache[slot.raw_material], slot, today, taken)
            if lot is None:
                result.shortages.append(f"{slot.name}: {slot.qty:g} {slot.unit} «{slot.raw_material}»")
            else:
                taken[lot["lot_id"]] = taken.get(lot["lot_id"], Decimal(0)) + slot.qty
            reservation_id = await session.scalar(text("""
                insert into shop.reservations (order_id, item_index, unit_no, part_no, sku_id, raw_material, lot_id, qty)
                values (:o, :i, :u, :p, :s, :r, :l, :q)
                on conflict (order_id, item_index, unit_no, part_no) do nothing returning id"""),
                {"o": order_id, "i": slot.item_index, "u": slot.unit_no, "p": slot.part_no, "s": slot.sku_id,
                 "r": slot.raw_material, "l": lot["lot_id"] if lot else None, "q": slot.qty})
            if reservation_id is None:
                continue
            result.reserved += 1
            if lot is not None:
                await session.execute(text("""
                    insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, created_by)
                    values (:l, 'резерв', :q, :r, :o, 'бот')"""),
                    {"l": lot["lot_id"], "q": slot.qty, "r": reservation_id, "o": order_id})
        await session.commit()
    if result.shortages:
        await _alert(f"⚠️ <b>Заказ №{order_id}: не хватает сырья</b> — заказ оформлен, резерв без партии\n"
                     + "\n".join(f"• {html.escape(s)}" for s in result.shortages[:10]))
    if result.unknown:
        logger.warning("Заказ %s: позиции без артикула каталога, сырьё не зарезервировано: %s",
                       order_id, result.unknown)
    return result


async def release_order(order_id: int, *, note: str = "заказ отменён или деньги возвращены") -> int:
    """Снять резерв заказа, ещё не собранного. Собранные пачки не трогаем."""
    if not enabled():
        return 0
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text(_LOCK))
        rows = (await session.execute(text("""
            update shop.reservations set status = 'снят', updated_at = now()
            where order_id = :o and status = 'резерв' returning id, lot_id, qty"""), {"o": order_id})).all()
        for row in rows:
            if row.lot_id is not None:
                await session.execute(text("""
                    insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, note, created_by)
                    values (:l, 'снятие резерва', :q, :r, :o, :n, 'бот')"""),
                    {"l": row.lot_id, "q": row.qty, "r": row.id, "o": order_id, "n": note})
        await session.commit()
    if rows:
        logger.info("Заказ %s: снят резерв сырья — %s мест", order_id, len(rows))
    return len(rows)


async def note_return(order_id: int) -> int:
    """Возврат после вручения: пачки списаны, в граммы не вернуть — запись «возврат»."""
    if not enabled():
        return 0
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text("""
            select r.id, r.lot_id, r.qty from shop.reservations r
            where r.order_id = :o and r.status = 'списан' and r.lot_id is not null
              and not exists (select 1 from shop.stock_moves m where m.reservation_id = r.id and m.kind = 'возврат')"""),
            {"o": order_id})).all()
        for row in rows:
            await session.execute(text("""
                insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, note, created_by)
                values (:l, 'возврат', :q, :r, :o, 'возврат после вручения: пачка не возвращается в граммы', 'бот')"""),
                {"l": row.lot_id, "q": row.qty, "r": row.id, "o": order_id})
        await session.commit()
    return len(rows)


# --- сборка ------------------------------------------------------------------------------


async def packing_view(order_id: int) -> list[dict]:
    """Из какой партии брать по каждой позиции и сырью — для страницы сборки."""
    if not enabled():
        return []
    today = _today()
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = [dict(r) for r in (await session.execute(text("""
            select r.item_index, r.raw_material, r.status, r.lot_id, l.lot_code, l.best_before,
                   c.kind, c.shelf_life_months, count(*) as places, sum(r.qty) as qty
            from shop.reservations r
            left join shop.lots l on l.id = r.lot_id
            join shop.catalog c on c.sku_id = r.sku_id
            where r.order_id = :o and r.status <> 'снят'
            group by r.item_index, r.raw_material, r.status, r.lot_id, l.lot_code, l.best_before,
                     c.kind, c.shelf_life_months
            order by r.item_index, r.raw_material, l.lot_code"""), {"o": order_id})).mappings()]
        lots: dict[str, list[dict]] = {}
        for raw in {row["raw_material"] for row in rows}:
            lots[raw] = [dict(c) for c in (await session.execute(text("""
                select lot_id, lot_code, free, best_before, unit from shop.v_lot_balance
                where raw_material = :raw and active and on_hand > 0
                order by best_before nulls last, arrival_date"""), {"raw": raw})).mappings()]
    view = []
    for row in rows:
        unit = "шт" if row["kind"] == "блин" else "г"
        until = pack_best_before(today, row["shelf_life_months"], row["best_before"]) if row["lot_id"] else None
        view.append({
            "index": row["item_index"], "raw_material": row["raw_material"], "status": row["status"],
            "unit": unit, "places": int(row["places"]), "qty": float(row["qty"]),
            "lot_id": row["lot_id"], "lot_code": row["lot_code"],
            "best_before": row["best_before"].strftime("%d.%m.%Y") if row["best_before"] else None,
            "pack_best_before": until.strftime("%d.%m.%Y") if until else None,
            "shelf_ok": bool(row["lot_id"]) and shelf_ok(today, row["shelf_life_months"], row["best_before"]),
            "choices": [
                {"lot_id": c["lot_id"], "lot_code": c["lot_code"], "free": float(c["free"]),
                 "best_before": c["best_before"].strftime("%d.%m.%Y") if c["best_before"] else None}
                for c in lots.get(row["raw_material"], [])
                if c["unit"] == unit and shelf_ok(today, row["shelf_life_months"], c["best_before"])
            ],
        })
    return view


async def change_lot(order_id: int, item_index: int, raw_material: str, lot_id: int) -> int:
    """Сборщик взял другую партию: перенести резерв позиции по этому сырью."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text(_LOCK))
        lot = (await session.execute(text(
            "select lot_id, lot_code, raw_material, unit, free, best_before, active from shop.v_lot_balance where lot_id = :l"),
            {"l": lot_id})).mappings().first()
        if lot is None or not lot["active"] or lot["raw_material"] != raw_material:
            raise StockError("Такой партии этого сырья нет.")
        rows = (await session.execute(text("""
            select r.id, r.lot_id, r.qty, c.shelf_life_months from shop.reservations r
            join shop.catalog c on c.sku_id = r.sku_id
            where r.order_id = :o and r.item_index = :i and r.raw_material = :raw and r.status = 'резерв'
              and r.lot_id is distinct from :l"""),
            {"o": order_id, "i": item_index, "raw": raw_material, "l": lot_id})).all()
        need = sum((Decimal(r.qty) for r in rows), Decimal(0))
        if Decimal(lot["free"]) < need:
            raise StockError(f"В партии {lot['lot_code']} свободно {lot['free']:g} {lot['unit']}, нужно {need:g}.")
        for row in rows:
            if not shelf_ok(_today(), row.shelf_life_months, lot["best_before"]):
                raise StockError(f"Из партии {lot['lot_code']} пачка выйдет со сроком меньше "
                                 f"{settings.min_shelf_life_days} дней.")
        for row in rows:
            if row.lot_id is not None:
                await session.execute(text("""
                    insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, note, created_by)
                    values (:l, 'снятие резерва', :q, :r, :o, 'сборщик взял другую партию', 'бот')"""),
                    {"l": row.lot_id, "q": row.qty, "r": row.id, "o": order_id})
            await session.execute(text(
                "update shop.reservations set lot_id = :l, updated_at = now() where id = :r"), {"l": lot_id, "r": row.id})
            await session.execute(text("""
                insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, note, created_by)
                values (:l, 'резерв', :q, :r, :o, 'перенос при сборке', 'бот')"""),
                {"l": lot_id, "q": row.qty, "r": row.id, "o": order_id})
        await session.commit()
    return len(rows)


async def write_off(order_id: int, *, by: str = "", weights: dict[int, float] | None = None) -> int:
    """«Собрано»: списать сырьё зарезервированных партий; перевес — в потери.

    `weights` — фактический вес позиции в граммах (необязательно, только у
    рассыпного): больше номинала — разница в потери фасовки той же партии.
    Позиция без партии или со сроком меньше допустимого — сборка не проходит.
    """
    if not enabled():
        return 0
    await reserve_order(order_id)  # заказ, оплаченный до склада по партиям, — резерв сейчас
    today = _today()
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text(_LOCK))
        rows = (await session.execute(text("""
            select r.id, r.item_index, r.raw_material, r.lot_id, r.qty, l.best_before, l.unit, l.lot_code,
                   c.shelf_life_months, c.name
            from shop.reservations r
            left join shop.lots l on l.id = r.lot_id
            join shop.catalog c on c.sku_id = r.sku_id
            where r.order_id = :o and r.status = 'резерв' order by r.item_index, r.unit_no, r.part_no"""),
            {"o": order_id})).all()
        problems = []
        for row in rows:
            if row.lot_id is None:
                problems.append(f"{row.name}: нет партии «{row.raw_material}» — внесите партию и выберите её")
            elif not shelf_ok(today, row.shelf_life_months, row.best_before):
                problems.append(f"{row.name}: партия {row.lot_code} — срок пачки меньше "
                                f"{settings.min_shelf_life_days} дней, возьмите другую")
        if problems:
            raise StockError("; ".join(dict.fromkeys(problems)))
        by = (by or "").strip()[:60] or "сборщик"
        nominal: dict[int, Decimal] = {}
        lot_of: dict[int, int] = {}
        for row in rows:
            until = pack_best_before(today, row.shelf_life_months, row.best_before)
            await session.execute(text("""
                update shop.reservations set status = 'списан', packed_at = now(), packed_by = :b,
                       pack_best_before = :u, updated_at = now() where id = :r"""),
                {"b": by, "u": until, "r": row.id})
            await session.execute(text("""
                insert into shop.stock_moves (lot_id, kind, qty, reservation_id, order_id, created_by)
                values (:l, 'списание', :q, :r, :o, :b)"""),
                {"l": row.lot_id, "q": row.qty, "r": row.id, "o": order_id, "b": by})
            if row.unit == "г":
                nominal[row.item_index] = nominal.get(row.item_index, Decimal(0)) + Decimal(row.qty)
                lot_of.setdefault(row.item_index, row.lot_id)
        for index, fact in (weights or {}).items():
            extra = Decimal(str(fact)) - nominal.get(index, Decimal(fact))
            if index in lot_of and extra > 0:
                await session.execute(text("""
                    insert into shop.stock_moves (lot_id, kind, qty, order_id, note, created_by)
                    values (:l, 'потери', :q, :o, :n, :b)"""),
                    {"l": lot_of[index], "q": extra, "o": order_id,
                     "n": f"фактический вес {fact:g} г против {nominal[index]:g} г", "b": by})
        await session.commit()
    return len(rows)


# --- для каталога --------------------------------------------------------------------------


async def free_by_raw(session) -> dict[tuple[str, str], Decimal]:
    """Свободное сырьё, из которого выйдет пачка с допустимым сроком: (сырьё, единица) → количество."""
    limit = _today() + timedelta(days=settings.min_shelf_life_days)
    rows = await session.execute(text("""
        select raw_material, unit, sum(free) as free from shop.v_lot_balance
        where active and free > 0 and (best_before is null or best_before >= :limit)
        group by raw_material, unit"""), {"limit": limit})
    return {(r.raw_material, r.unit): Decimal(r.free) for r in rows}


def enough(article: dict, components: list[dict], free: dict[tuple[str, str], Decimal]) -> bool:
    """Хватит ли сырья на одну единицу артикула (у набора — на все вложения)."""
    need: dict[tuple[str, str], Decimal] = {}
    for component in components or [article]:
        qty, unit = _need(component)
        key = (component.get("raw_material") or "", unit)
        need[key] = need.get(key, Decimal(0)) + qty
    return bool(need) and all(free.get(key, Decimal(0)) >= qty for key, qty in need.items())


async def _alert(message: str) -> None:
    if not settings.telegram_ops_chat_id:
        logger.warning("%s", message)
        return
    from app.messages import manager as manager_messages

    try:
        await manager_messages.notify(manager_messages.STOCK, message, chat_id=settings.telegram_ops_chat_id)
    except Exception:
        logger.exception("Тревогу склада в Ops не отправили")
