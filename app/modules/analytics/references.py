"""Справочники из Google Таблицы → база → Metabase (маржа заказов).

Вкладки «Товары», «Состав», «Себестоимость», «Настройки», «Приходы и
инвентаризация» лежат в той же таблице, что и каталог бота
(`CATALOG_SHEET_ID`), и читаются тем же сервисным аккаунтом (только
чтение). Раз в 15 минут, из тика расписания; руками — `scripts/api.sh
references/sync`.

Правила — те, что написаны владельцем на вкладке «Справочники — как
пользоваться», и код их держит:

- **плохая строка пропускается, а прежнее значение остаётся.** Плохая —
  пустое обязательное значение, отрицательное число, неизвестный код
  товара, цена или себестоимость, выросшая больше чем в 3 раза. Про каждую
  — сообщение в Ops-чат, одно на каждый новый набор проблем, а не каждые 15
  минут;
- **себестоимость и настройки меняются новой строкой с датой «Действует
  с»**, старые не удаляются: маржа заказа берёт значение на дату оплаты.
  Исправление опечатки — правка той же строки, она перезаписывается;
- **строку убрали из таблицы — её нет и в базе**: таблица — источник
  правды (так уходят тестовые «Приходы» перед настоящим приходом). Вкладка
  пропала или без нужных заголовков — её данные в базе не трогаем.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.core import google_sheets
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.analytics.models import (
    RefComposition, RefCost, RefProduct, RefSetting, RefStockMove, RefSync,
)

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=15)
# Цена или себестоимость выросла больше чем во столько раз — скорее опечатка
# (лишний ноль), чем подорожание: строку не применяем, спрашиваем человека.
_MAX_GROWTH = Decimal(3)
NOT_IN_CATALOG = "нет в каталоге"

TABS = {
    "products": "Товары",
    "composition": "Состав",
    "costs": "Себестоимость",
    "settings": "Настройки",
    "moves": "Приходы и инвентаризация",
}

# Заголовки столбцов — как их пишет человек; регистр и пробелы не важны.
HEADERS = {
    "products": {"код": "code", "название": "name", "тип": "kind", "вес, г": "weight_g",
                 "розничная цена, ₽": "retail_price", "артикул поставщика": "supplier_sku",
                 "роль": "role", "активен": "active", "комментарий": "comment",
                 "название в каталоге бота": "bot_name"},
    "composition": {"код продукта": "product_code", "код сорта": "component_code",
                    "количество": "quantity", "ед.": "unit", "упаковка компонента": "packaging"},
    "costs": {"код": "code", "себестоимость единицы, ₽": "unit_cost", "действует с": "valid_from",
              "источник": "source", "комментарий": "comment"},
    "settings": {"ключ": "key", "параметр": "name", "значение": "value", "ед.": "unit",
                 "действует с": "valid_from", "источник": "source", "комментарий": "comment"},
    "moves": {"дата": "move_date", "операция": "operation", "код сорта": "code", "количество": "quantity",
              "ед.": "unit", "сумма партии, ₽": "amount", "документ": "document", "комментарий": "comment"},
}
REQUIRED = {
    "products": ("code", "name"),
    "composition": ("product_code", "component_code", "quantity"),
    "costs": ("code", "unit_cost", "valid_from"),
    "settings": ("key", "value", "valid_from"),
    "moves": ("move_date", "operation", "code", "quantity"),
}

_YES = {"да", "есть", "1", "+", "yes", "true"}
_NO = {"нет", "0", "-", "no", "false"}


class BadValue(ValueError):
    """Значение ячейки не годится. Текст — для человека."""


def number(raw: str, *, percent_ok: bool = False) -> Decimal:
    """Число в русском формате: «1 020 ₽», «91,95 ₽», «1,565», «3,4%» (→ 0.034)."""
    text = (raw or "").strip()
    if not text:
        raise BadValue("пусто")
    is_percent = text.endswith("%")
    cleaned = re.sub(r"[\s  ₽руб.]+$", "", text.rstrip("%"), flags=re.I)
    cleaned = re.sub(r"[\s  ]", "", cleaned).replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise BadValue(f"«{raw}» — не число") from None
    if value < 0:
        raise BadValue(f"«{raw}» — отрицательное число")
    if is_percent:
        if not percent_ok:
            raise BadValue(f"«{raw}» — здесь нужна сумма, а не процент")
        value = value / 100
    return value


def day(raw: str) -> date:
    text = (raw or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise BadValue(f"«{raw}» — не дата (нужно ДД.ММ.ГГГГ)" if text else "пусто")


def _flag(raw: str) -> bool:
    text = (raw or "").strip().casefold()
    if text in _YES or not text:
        return True
    if text in _NO:
        return False
    raise BadValue(f"«{raw}» — нужно «да» или «нет»")


def _text(raw: str) -> str | None:
    text = (raw or "").strip()
    return text or None


@dataclass
class Tab:
    """Строки вкладки как словари по нашим именам столбцов."""

    rows: list[tuple[int, dict]] = field(default_factory=list)
    problem: str = ""  # вкладку целиком не читаем — и данные её в базе не трогаем


def read_tab(kind: str, rows: list[list[str]]) -> Tab:
    title = TABS[kind]
    if not rows:
        return Tab(problem=f"«{title}»: вкладка пустая или её нет")
    header = [HEADERS[kind].get(" ".join(cell.split()).casefold()) for cell in rows[0]]
    missing = [name for name in REQUIRED[kind] if name not in header]
    if missing:
        names = {v: k for k, v in HEADERS[kind].items()}
        return Tab(problem=f"«{title}»: нет столбцов {', '.join('«' + names[m] + '»' for m in missing)}")
    tab = Tab()
    for number_, row in enumerate(rows[1:], start=2):
        values = {key: (row[i] if i < len(row) else "") for i, key in enumerate(header) if key}
        if not any((v or "").strip() for v in values.values()):
            continue
        tab.rows.append((number_, values))
    return tab


@dataclass
class Plan:
    """Что применить: строки по ключу и ключи, которые оставить как есть."""

    upsert: dict[tuple, dict] = field(default_factory=dict)
    keep: set[tuple] = field(default_factory=set)  # строка есть, но плохая — прежнее значение остаётся


@dataclass
class Result:
    plans: dict[str, Plan] = field(default_factory=dict)
    skipped_tabs: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


def plan(tabs: dict[str, Tab], previous: dict[str, dict[tuple, Decimal]]) -> Result:
    """Разобрать вкладки и решить, что применить. Без базы — чтобы проверять тестами.

    `previous` — значения в базе для правила «выросло больше чем в 3 раза»:
    `{"products": {(code,): retail_price}, "costs": {(code, valid_from): unit_cost}}`.
    """
    result = Result()
    for kind, tab in tabs.items():
        if tab.problem:
            result.problems.append(tab.problem)
            result.skipped_tabs.add(kind)

    def bad(kind: str, row: int, what: str, key: tuple | None) -> None:
        result.problems.append(f"«{TABS[kind]}», строка {row}: {what} — строка пропущена"
                               + (", осталось прежнее значение" if key else ""))
        if key:
            result.plans[kind].keep.add(key)

    for kind in TABS:
        result.plans[kind] = Plan()

    # Товары — первыми: остальные вкладки проверяются по их кодам.
    known: set[str] = set()
    for row, v in tabs.get("products", Tab()).rows:
        code = (v.get("code") or "").strip()
        if not code:
            bad("products", row, "нет кода", None)
            continue
        known.add(code)
        key = (code,)
        try:
            if not _text(v.get("name")):
                raise BadValue("нет названия")
            price = number(v["retail_price"]) if _text(v.get("retail_price")) else None
            old = previous.get("products", {}).get(key)
            if price is not None and old and price > old * _MAX_GROWTH:
                raise BadValue(f"цена {price} ₽ выросла больше чем в 3 раза (было {old} ₽)")
            bot_name = _text(v.get("bot_name"))
            result.plans["products"].upsert[key] = {
                "code": code, "name": _text(v["name"]), "kind": _text(v.get("kind")),
                "weight_g": number(v["weight_g"]) if _text(v.get("weight_g")) else None,
                "retail_price": price, "supplier_sku": _text(v.get("supplier_sku")),
                "role": _text(v.get("role")), "active": _flag(v.get("active", "")),
                "comment": _text(v.get("comment")),
                "bot_name": None if not bot_name or bot_name.casefold() == NOT_IN_CATALOG else bot_name,
            }
        except BadValue as error:
            bad("products", row, str(error), key)
    if "products" in result.skipped_tabs:
        # Товаров не прочитали — коды сверять не с чем, берём известные базе.
        known = {key[0] for key in previous.get("products", {})} | set(previous.get("_codes", set()))

    def need_known(code: str, what: str = "код") -> None:
        if code not in known:
            raise BadValue(f"{what} «{code}» не найден на вкладке «Товары»")

    for row, v in tabs.get("composition", Tab()).rows:
        key = ((v.get("product_code") or "").strip(), (v.get("component_code") or "").strip())
        try:
            need_known(key[0], "код продукта")
            need_known(key[1], "код сорта")
            quantity = number(v["quantity"])
            if not quantity:
                raise BadValue("количество 0")
            result.plans["composition"].upsert[key] = {
                "product_code": key[0], "component_code": key[1], "quantity": quantity,
                "unit": _text(v.get("unit")), "packaging": _text(v.get("packaging")),
            }
        except BadValue as error:
            bad("composition", row, str(error), key if all(key) else None)

    for row, v in tabs.get("costs", Tab()).rows:
        code = (v.get("code") or "").strip()
        try:
            valid_from = day(v.get("valid_from", ""))
        except BadValue as error:
            bad("costs", row, f"«Действует с»: {error}", None)
            continue
        key = (code, valid_from)
        try:
            need_known(code)
            cost = number(v["unit_cost"])
            if not cost:
                raise BadValue("себестоимость 0")
            old = previous.get("costs", {}).get(key) or _latest_before(previous.get("costs", {}), code, valid_from)
            if old and cost > old * _MAX_GROWTH:
                raise BadValue(f"себестоимость {cost} ₽ выросла больше чем в 3 раза (было {old} ₽)")
            result.plans["costs"].upsert[key] = {
                "code": code, "valid_from": valid_from, "unit_cost": cost,
                "source": _text(v.get("source")), "comment": _text(v.get("comment")),
            }
        except BadValue as error:
            bad("costs", row, str(error), key if code else None)

    for row, v in tabs.get("settings", Tab()).rows:
        key_name = (v.get("key") or "").strip()
        value_raw, date_raw = (v.get("value") or "").strip(), (v.get("valid_from") or "").strip()
        if key_name and not value_raw and not date_raw:
            # Параметр заведён, но не заполнен («Нет данных. Замерить…») — это не ошибка.
            continue
        try:
            if not key_name:
                raise BadValue("нет ключа")
            valid_from = day(date_raw)
        except BadValue as error:
            bad("settings", row, str(error), None)
            continue
        key = (key_name, valid_from)
        try:
            unit = (_text(v.get("unit")) or "").casefold()
            value = number(value_raw, percent_ok=True)
            if unit == "доля" and not value_raw.endswith("%") and value > 1:
                raise BadValue(f"«{value_raw}» — доля больше 1, нужен процент со знаком %")
            result.plans["settings"].upsert[key] = {
                "key": key_name, "valid_from": valid_from, "name": _text(v.get("name")), "value": value,
                "unit": "доля" if value_raw.endswith("%") else _text(v.get("unit")),
                "source": _text(v.get("source")), "comment": _text(v.get("comment")),
            }
        except BadValue as error:
            bad("settings", row, str(error), key)

    for row, v in tabs.get("moves", Tab()).rows:
        code = (v.get("code") or "").strip()
        try:
            move_date = day(v.get("move_date", ""))
            operation = _text(v.get("operation"))
            if not operation:
                raise BadValue("нет операции")
        except BadValue as error:
            bad("moves", row, str(error), None)
            continue
        key = (move_date, operation, code, (v.get("document") or "").strip())
        try:
            need_known(code, "код сорта")
            result.plans["moves"].upsert[key] = {
                "move_date": move_date, "operation": operation, "code": code, "document": key[3],
                "quantity": number(v["quantity"]), "unit": _text(v.get("unit")),
                "amount": number(v["amount"]) if _text(v.get("amount")) else None,
                "comment": _text(v.get("comment")),
            }
        except BadValue as error:
            bad("moves", row, str(error), key if code else None)
    return result


def _latest_before(costs: dict[tuple, Decimal], code: str, valid_from: date) -> Decimal | None:
    earlier = [(d, v) for (c, d), v in costs.items() if c == code and d <= valid_from]
    return max(earlier)[1] if earlier else None


_MODELS = {
    "products": (RefProduct, ("code",)),
    "composition": (RefComposition, ("product_code", "component_code")),
    "costs": (RefCost, ("code", "valid_from")),
    "settings": (RefSetting, ("key", "valid_from")),
    "moves": (RefStockMove, ("move_date", "operation", "code", "document")),
}


async def _previous(session) -> dict:
    prices = {(r.code,): r.retail_price for r in (await session.execute(select(RefProduct))).scalars()
              if r.retail_price is not None}
    costs = {(r.code, r.valid_from): r.unit_cost for r in (await session.execute(select(RefCost))).scalars()}
    codes = set((await session.execute(select(RefProduct.code))).scalars())
    return {"products": prices, "costs": costs, "_codes": codes}


async def _apply(session, result: Result, now: datetime) -> dict:
    counts = {}
    for kind, (model, key_cols) in _MODELS.items():
        if kind in result.skipped_tabs:
            counts[TABS[kind]] = "не тронута"
            continue
        current = result.plans[kind]
        existing = set(tuple(row) for row in (await session.execute(
            select(*[getattr(model, c) for c in key_cols]))).all())
        # Строки, которых в таблице больше нет совсем, уходят; плохие — остаются прежними.
        gone = existing - set(current.upsert) - current.keep
        for key in gone:
            await session.execute(delete(model).where(*[getattr(model, c) == v for c, v in zip(key_cols, key)]))
        for values in current.upsert.values():
            row = {**values, "synced_at": now}
            await session.execute(
                insert(model).values(**row).on_conflict_do_update(
                    index_elements=list(key_cols), set_={k: v for k, v in row.items() if k not in key_cols})
            )
        counts[TABS[kind]] = {"строк": len(current.upsert), "оставлено прежних": len(current.keep & existing),
                              "удалено": len(gone)}
    return counts


async def _tell_ops(problems: list[str]) -> bool:
    """Сообщение в Ops-чат — одно на каждый новый набор проблем."""
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages

    digest = hashlib.sha256("\n".join(sorted(problems)).encode()).hexdigest()[:16]
    if not await client_messages.claim_once(f"references:{digest}", "references_problem", 0):
        return False
    await manager_messages.notify(
        manager_messages.REFERENCES,
        "⚠️ <b>Справочники в таблице: строки не применены</b>\n"
        + "\n".join(f"• {html.escape(p)}" for p in problems[:20])
        + ("\n…" if len(problems) > 20 else "")
        + "\nВ Metabase для них осталось прежнее значение.",
        chat_id=settings.telegram_ops_chat_id or None,
    )
    return True


async def sync(*, force: bool = False, now: datetime | None = None) -> dict:
    """Прочитать справочники и разложить в базу. Раз в 15 минут; `force` — сейчас."""
    from app.modules.catalog import sheet as catalog_sheet

    if not settings.references_sync_enabled:
        return {"skipped": "загрузка справочников выключена (REFERENCES_SYNC_ENABLED)"}
    if catalog_sheet._mode() != catalog_sheet.SERVICE_ACCOUNT or not settings.catalog_sheet_id:
        return {"skipped": "справочники читаются только сервисным аккаунтом (SHEETS_AUTH_MODE, CATALOG_SHEET_ID)"}
    now = now or datetime.now(timezone.utc)
    session_factory = get_session_factory()
    async with session_factory() as session:
        state = await session.get(RefSync, 1)
        if not force and state and state.checked_at and now - state.checked_at < INTERVAL:
            return {"skipped": "читали меньше 15 минут назад"}

    try:
        ranges = await google_sheets.client().batch_get(
            settings.catalog_sheet_id, [google_sheets.a1_range(title) for title in TABS.values()]
        )
    except google_sheets.SheetsError as error:
        if error.transient:
            logger.warning("Справочники не прочитали: %s", error)
            return {"failed": str(error)}
        logger.error("Справочники не прочитать сервисным аккаунтом: %s", error)
        await _tell_ops([f"таблицу не прочитать: {error}"])
        return {"failed": str(error)}

    tabs = {kind: read_tab(kind, google_sheets.pad_rows(rows)) for kind, rows in zip(TABS, ranges)}
    async with session_factory() as session:
        result = plan(tabs, await _previous(session))
        counts = await _apply(session, result, now)
        state = await session.get(RefSync, 1) or RefSync(id=1)
        state.checked_at = now
        state.synced_at = now
        state.last_problems = "\n".join(result.problems)[:4000] or None
        session.add(state)
        await session.commit()

    if result.problems:
        logger.warning("Справочники: %s проблем(ы): %s", len(result.problems), "; ".join(result.problems)[:500])
        await _tell_ops(result.problems)
    return {"применено": counts, "проблем": len(result.problems), "проблемы": result.problems[:20]}
