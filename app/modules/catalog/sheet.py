"""Каталог из Google Таблицы: менеджер правит цену — бот знает её без деплоя.

Таблица публикуется в интернете как CSV («Файл → Поделиться →
Опубликовать в интернете → CSV»), адрес лежит в `CATALOG_SHEET_CSV_URL`.
Ключи Google не нужны: цены и описания и так публичные.

Устроено в два шага, и это не лишнее. Тик расписания раз в пять минут
скачивает таблицу, проверяет её и кладёт удачную версию в базу
(`refresh`). Каждый запрос к контейнеру перед обработкой подтягивает эту
версию из базы в память, не чаще раза в минуту (`ensure_fresh`). Скачивать
таблицу из Google в пути сообщения клиента нельзя: у вебхука ВК на всё
около восьми секунд.

**Таблица с ошибкой не применяется целиком.** Пустая цена или кривой GTIN
в одной строке — и бот продолжает работать на прошлой удачной версии, а
менеджеру уходит 🚨 со списком ошибок, один раз на каждую новую ошибку.
Применять таблицу построчно, выбрасывая сломанные строки, опаснее: товар
тихо пропал бы из продажи. Пока таблица ни разу не читалась, бот работает
по `catalog.json` из образа.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import manager as manager_messages
from app.modules.catalog.models import CatalogSnapshot

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
# Как часто контейнер сверяет свою память с базой. Сверяется только
# отпечаток таблицы — одна короткая строка по первичному ключу, — а список
# товаров перечитывается, лишь когда отпечаток сменился. Раньше здесь была
# минута и полная перечитка: 27.09.2026 таблицу дополнили, прочитали
# командой, а бот ещё минуту отвечал по старому списку — клиент спросил
# ассортимент и не услышал про новый чай.
_MEMORY_TTL_SECONDS = 5

# Заголовки столбцов — как их пишет человек. Регистр и пробелы не важны.
_COLUMNS = {
    "название": "name",
    "товар": "name",
    "цена": "price",
    "фасовка": "package_sizes",
    "фасовки": "package_sizes",
    "в наличии": "in_stock",
    "наличие": "in_stock",
    "gtin": "gtin",
    "ссылка": "link",
    "описание": "description",
    "с чем советуем": "recommended",
    "синонимы": "synonyms",
    "другие названия": "synonyms",
}
_REQUIRED = ("name", "price")

_YES = {"да", "есть", "1", "+", "yes", "true", "в наличии"}
_NO = {"нет", "0", "-", "no", "false", "нет в наличии"}


@dataclass
class Parsed:
    items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _price(raw: str) -> float | None:
    text = re.sub(r"[^\d,.]", "", raw or "").replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_csv(text: str) -> Parsed:
    """Строки таблицы → товары каталога в том же виде, что `catalog.json`."""
    from app.modules.marking import codes

    result = Parsed()
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    rows = list(reader)
    if not rows:
        result.errors.append("таблица пустая")
        return result

    header = [_COLUMNS.get(cell.strip().lower()) for cell in rows[0]]
    missing = [name for name in _REQUIRED if name not in header]
    if missing:
        titles = {"name": "Название", "price": "Цена"}
        result.errors.append(
            "нет столбцов: " + ", ".join(titles[name] for name in missing)
            + " (заголовки — в первой строке)"
        )
        return result

    seen: set[str] = set()
    for number, row in enumerate(rows[1:], start=2):
        values = {
            key: (row[i].strip() if i < len(row) else "")
            for i, key in enumerate(header)
            if key
        }
        if not any(values.values()):
            continue  # пустая строка между товарами — не ошибка

        where = f"строка {number}"
        name = values.get("name", "")
        if not name:
            result.errors.append(f"{where}: нет названия")
            continue
        where = f"строка {number} «{name}»"
        if name.casefold() in seen:
            result.errors.append(f"{where}: такое название уже есть выше")
            continue
        seen.add(name.casefold())

        price = _price(values.get("price", ""))
        if price is None:
            result.errors.append(f"{where}: цена «{values.get('price', '')}» — не число больше нуля")
            continue

        stock_raw = values.get("in_stock", "").lower()
        if stock_raw in _YES or stock_raw == "":
            in_stock = True
        elif stock_raw in _NO:
            in_stock = False
        else:
            result.errors.append(f"{where}: «В наличии» — «{stock_raw}», нужно «да» или «нет»")
            continue

        gtin_raw = values.get("gtin", "")
        gtin = codes.normalize_gtin(gtin_raw) if gtin_raw else ""
        if gtin and not codes.gtin_is_valid(gtin):
            result.errors.append(
                f"{where}: GTIN «{gtin_raw}» с ошибкой — не сходится контрольная цифра"
            )
            continue

        sizes = [
            part.strip()
            for part in re.split(r"[;,]", values.get("package_sizes", ""))
            if part.strip()
        ]
        item = {
            "name": name,
            # Целые рубли храним целыми: в тексте клиенту «100 руб.», а не
            # «100.0 руб.».
            "price": int(price) if price.is_integer() else price,
            "package_sizes": sizes,
            "in_stock": in_stock,
            "gtin": gtin,
            "link": values.get("link", ""),
            "description": values.get("description", ""),
            "recommended": [
                part.strip()
                for part in values.get("recommended", "").split(",")
                if part.strip()
            ],
            # Как ещё называют товар — чтобы кнопка «Взять» нашла его в
            # ответе модели, написанном не по таблице.
            "synonyms": [
                part.strip() for part in re.split(r"[;,]", values.get("synonyms", "")) if part.strip()
            ],
            "_row": number,
        }
        result.items.append(item)

    # «С чем советуем» ссылается на названия из той же таблицы, поэтому
    # проверяется, когда прочитаны все строки. Опечатка в названии — такая
    # же ошибка, как пустая цена: иначе бот советовал бы то, чего нет.
    names = {item["name"].casefold(): item["name"] for item in result.items}
    for item in result.items:
        row = item.pop("_row")
        unknown = [name for name in item["recommended"] if name.casefold() not in names]
        if unknown:
            result.errors.append(
                f"строка {row} «{item['name']}»: в «С чем советуем» нет такого "
                f"товара в таблице — {', '.join(unknown)}"
            )
            continue
        item["recommended"] = [names[name.casefold()] for name in item["recommended"]]

    if not result.items and not result.errors:
        result.errors.append("в таблице нет ни одного товара")
    return result


# То, что сейчас в памяти контейнера. None — таблица не подключена или ещё
# не читалась: тогда работает `catalog.json`.
_memory: list[dict] | None = None
_memory_hash: str | None = None
_memory_loaded_at = 0.0


def current_items() -> list[dict] | None:
    """Товары из таблицы или None, если бот сейчас работает по `catalog.json`."""
    return [dict(item) for item in _memory] if _memory is not None else None


def _remember(items: list[dict] | None, source_hash: str | None = None) -> None:
    global _memory, _memory_hash, _memory_loaded_at
    _memory = list(items) if items else None
    _memory_hash = source_hash if items else None
    _memory_loaded_at = time.monotonic()


async def ensure_fresh() -> None:
    """Подтянуть из базы версию, которую положил тик или команда.

    Каждые несколько секунд сверяем только отпечаток; товары читаем, когда
    он сменился.
    """
    global _memory_loaded_at
    if not settings.catalog_sheet_csv_url:
        return
    if _memory_loaded_at and time.monotonic() - _memory_loaded_at < _MEMORY_TTL_SECONDS:
        return
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            stored_hash = await session.scalar(
                select(CatalogSnapshot.source_hash).where(CatalogSnapshot.id == 1)
            )
            if stored_hash is not None and stored_hash == _memory_hash:
                _memory_loaded_at = time.monotonic()
                return
            row = await session.get(CatalogSnapshot, 1)
    except Exception:
        # Без базы остаётся то, что уже в памяти, или `catalog.json`.
        logger.warning("Каталог из таблицы не подтянули из базы", exc_info=True)
        return
    if row is not None and row.items:
        _remember(row.items, row.source_hash)
    else:
        _remember(None)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def refresh(*, force: bool = False) -> dict:
    """Скачать таблицу, проверить и, если она в порядке, сделать её каталогом."""
    url = settings.catalog_sheet_csv_url
    if not url:
        return {"skipped": "CATALOG_SHEET_CSV_URL не задан — работает catalog.json"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig")
    except Exception as error:
        # Google не ответил — не повод трогать каталог и не повод будить
        # менеджера: следующий тик попробует снова.
        logger.warning("Таблицу каталога не скачали: %s: %s", type(error).__name__, error)
        return {"failed": f"таблицу не скачали: {type(error).__name__}"}

    if "<html" in text[:500].lower():
        # Ссылка не на CSV, а на страницу таблицы: такое бывает, если
        # скопировать адрес из браузера вместо «Опубликовать в интернете».
        parsed = Parsed(errors=["по ссылке не CSV, а веб-страница — нужна ссылка из «Опубликовать в интернете → CSV»"])
    else:
        parsed = parse_csv(text)

    source_hash = _hash(text)
    now = datetime.now(timezone.utc)
    session_factory = get_session_factory()
    async with session_factory() as session:
        row = await session.get(CatalogSnapshot, 1)
        if row is None:
            row = CatalogSnapshot(id=1, items=[])
            session.add(row)
        row.checked_at = now

        if parsed.errors:
            error_text = "; ".join(parsed.errors)
            error_hash = _hash(error_text)
            is_new = row.last_error_hash != error_hash
            row.last_error = error_text[:2000]
            row.last_error_hash = error_hash
            await session.commit()
            if is_new:
                await manager_messages.notify(
                    manager_messages.CATALOG_SHEET,
                    "🚨 <b>Таблица каталога с ошибками — бот работает на прошлой версии</b>\n"
                    + "\n".join(f"• {error}" for error in parsed.errors[:15])
                    + ("\n…" if len(parsed.errors) > 15 else ""),
                )
            logger.warning("Таблица каталога не применена: %s", error_text[:500])
            return {"applied": False, "errors": parsed.errors}

        changed = force or row.source_hash != source_hash
        if changed:
            row.items = parsed.items
            row.source_hash = source_hash
            row.updated_at = now
        row.last_error = None
        row.last_error_hash = None
        await session.commit()

    _remember(parsed.items, source_hash)
    return {
        "applied": True,
        "changed": changed,
        "товаров": len(parsed.items),
        "в наличии": sum(1 for item in parsed.items if item["in_stock"]),
    }
