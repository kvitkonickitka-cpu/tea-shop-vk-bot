"""Хранилище меток: какая метка у какого значения, по клиенту.

Записи клиента читаются из базы целиком и держатся в памяти процесса
недолго: за ход метки спрашиваются десяток раз (сообщение, промпт,
результаты инструментов, история), и каждый раз ходить в базу незачем.
Экземпляров контейнера несколько, поэтому кэш короткий, а незнакомую метку
перед отказом ищем в базе ещё раз — её мог завести соседний экземпляр.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.privacy import crypto
from app.privacy.models import PiiEntry

logger = logging.getLogger(__name__)

KINDS = ("NAME", "PHONE", "EMAIL", "ADDR", "GEO")
_TTL_SECONDS = 120


@dataclass
class Book:
    """Метки одного клиента."""

    values: dict[str, tuple[str, str]] = field(default_factory=dict)  # метка → (вид, значение)
    by_hash: dict[str, str] = field(default_factory=dict)  # отпечаток → метка
    loaded_at: float = 0.0

    def next_label(self, kind: str) -> str:
        numbers = [int(label.rsplit("_", 1)[1]) for label in self.values if label.startswith(kind + "_")]
        return f"{kind}_{max(numbers, default=0) + 1}"


_books: dict[str, Book] = {}
# Без базы (локальный запуск) — только память процесса.
_memory_only: dict[str, Book] = {}
# Пробный прогон переноса истории: метки раздаются в памяти, в базу не пишутся.
_simulated: dict[str, Book] | None = None


@contextlib.contextmanager
def simulate():
    global _simulated
    _simulated = {}
    try:
        yield
    finally:
        _simulated = None


def _decrypt_rows(client_key: str, rows) -> Book:
    book = Book(loaded_at=time.monotonic())
    for row in rows:
        try:
            value = crypto.decrypt(row.value_enc, client_key=client_key, label=row.label)
        except Exception:
            # Строку не расшифровать — ключ сменили или запись испорчена.
            # Метку не подставим; значение в лог не пишем, его и нет.
            logger.error("Метка %s не расшифровалась — ключ PII_ENCRYPTION_KEY другой?", row.label)
            continue
        book.values[row.label] = (row.kind, value)
        book.by_hash[row.value_hash] = row.label
    return book


async def load(client_key: str, *, fresh: bool = False) -> Book:
    if _simulated is not None and client_key in _simulated:
        return _simulated[client_key]
    book = _books.get(client_key)
    if book is not None and not fresh and time.monotonic() - book.loaded_at < _TTL_SECONDS:
        return book
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return _memory_only.setdefault(client_key, Book())
    async with session_factory() as session:
        rows = (await session.execute(
            select(PiiEntry).where(PiiEntry.client_key == client_key).order_by(PiiEntry.id)
        )).scalars().all()
    book = _decrypt_rows(client_key, rows)
    if _simulated is not None:
        _simulated[client_key] = book
        return book
    _books[client_key] = book
    return book


async def label_for(client_key: str, kind: str, value: str) -> str:
    """Метка значения у клиента: прежняя, если значение уже было, иначе новая."""
    digest = crypto.fingerprint(kind, value, client_key=client_key)
    book = await load(client_key)
    if digest in book.by_hash:
        return book.by_hash[digest]
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        session_factory = None
    if session_factory is None or _simulated is not None:
        label = book.next_label(kind)
        book.values[label] = (kind, value)
        book.by_hash[digest] = label
        return label

    for _ in range(5):
        label = book.next_label(kind)
        statement = insert(PiiEntry).values(
            client_key=client_key, label=label, kind=kind, value_hash=digest,
            value_enc=crypto.encrypt(value, client_key=client_key, label=label),
        ).on_conflict_do_nothing()
        async with session_factory() as session:
            inserted = (await session.execute(statement.returning(PiiEntry.id))).first()
            await session.commit()
        if inserted is not None:
            book.values[label] = (kind, value)
            book.by_hash[digest] = label
            return label
        # Соседний экземпляр успел завести эту метку или это значение.
        book = await load(client_key, fresh=True)
        if digest in book.by_hash:
            return book.by_hash[digest]
    raise RuntimeError("Не завели метку: пять раз подряд номер был занят")


async def value_of(client_key: str, label: str) -> str | None:
    book = await load(client_key)
    if label not in book.values:
        book = await load(client_key, fresh=True)
    found = book.values.get(label)
    return found[1] if found else None


def forget_cache() -> None:
    """Для тестов: база очищена — кэш тоже."""
    _books.clear()
    _memory_only.clear()


async def forget_kind_older_than(kind: str, hours: float) -> int:
    """Удалить метки вида старше `hours` часов — для геопозиции, которая нужна только на время выбора."""
    from sqlalchemy import delete, func, text

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return 0
    async with session_factory() as session:
        result = await session.execute(
            delete(PiiEntry).where(
                PiiEntry.kind == kind,
                PiiEntry.created_at < func.now() - text(f"interval '{float(hours)} hours'"),
            )
        )
        await session.commit()
    if result.rowcount:
        # Кэш метки сбрасываем целиком: какие клиенты затронуты, не знаем.
        _books.clear()
    return result.rowcount or 0
