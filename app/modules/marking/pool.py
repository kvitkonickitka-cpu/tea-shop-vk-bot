"""Пул выпущенных кодов: импорт выгрузки из СУЗ «Честного знака».

Импорт необязателен. Пока его не делали, при сборке принимается любой код,
годный по формату, и помечается «не из пула». Как только импортирован хоть
один код, пул считается заведённым: код, которого в нём нет, при сборке не
принимается — это либо чужая пачка, либо ошибка печати.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.modules.marking import codes
from app.modules.marking.models import IN_STOCK, MarkingCodeRow

logger = logging.getLogger(__name__)

_CHUNK = 500


async def pool_imported() -> bool:
    session_factory = get_session_factory()
    async with session_factory() as session:
        return bool(
            await session.scalar(select(exists().where(MarkingCodeRow.from_pool.is_(True))))
        )


def _candidates(line: str) -> list[str]:
    """Что в строке выгрузки может быть кодом.

    СУЗ отдаёт TXT (код на строку) и CSV. В CSV код бывает в кавычках и
    соседствует с другими колонками. Делить строку по запятой вслепую
    нельзя: в серийном номере «Честного знака» бывают и запятые, и точки с
    запятой. Поэтому сначала пробуем строку целиком, потом — поля CSV.
    """
    found = [line]
    for delimiter in (",", ";", "\t"):
        if delimiter in line:
            try:
                found.extend(next(csv.reader(io.StringIO(line), delimiter=delimiter)))
            except (csv.Error, StopIteration):
                continue
    return found


def parse_export(text: str, *, skip: str = "") -> tuple[list[codes.MarkingCode], list[str]]:
    """Коды из выгрузки и ошибки по строкам. `skip` — строка, которую не
    разбирать и не упоминать в ошибках (токен из тела запроса)."""
    parsed: list[codes.MarkingCode] = []
    errors: list[str] = []
    # Только по «\n», не `splitlines()`: тот считает концом строки и 0x1D —
    # тот самый разделитель GS внутри кода, — и резал каждый код на части.
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line.strip(" \t") or (skip and skip in line):
            continue
        code = None
        last_error = ""
        for candidate in _candidates(line):
            try:
                code = codes.parse(candidate)
                break
            except codes.CodeError as error:
                last_error = str(error)
        if code is None:
            # Сам код в ответ не кладём целиком: хватит начала, чтобы найти
            # строку в файле.
            errors.append(f"строка {number} ({codes.printable(line)[:24]}…): {last_error}")
            continue
        parsed.append(code)
    return parsed, errors


async def import_codes(text: str, *, skip: str = "") -> dict:
    parsed, errors = parse_export(text, skip=skip)
    now = datetime.now(timezone.utc)
    added = 0
    session_factory = get_session_factory()
    async with session_factory() as session:
        for start in range(0, len(parsed), _CHUNK):
            chunk = parsed[start:start + _CHUNK]
            result = await session.execute(
                insert(MarkingCodeRow)
                .values([
                    {
                        "code": code.code, "gtin": code.gtin, "serial": code.serial,
                        "status": IN_STOCK, "from_pool": True, "imported_at": now,
                        "manual": False,
                    }
                    for code in chunk
                ])
                # Повторный импорт той же выгрузки не должен ни падать, ни
                # плодить строки. Код, уже увиденный при сборке, в пул не
                # переводим: его судьба уже известна.
                .on_conflict_do_nothing()
                .returning(MarkingCodeRow.id)
            )
            added += len(result.scalars().all())
        await session.commit()

    logger.info(
        "Импорт кодов маркировки: разобрано %s, добавлено %s, ошибок %s",
        len(parsed), added, len(errors),
    )
    return {
        "разобрано": len(parsed),
        "добавлено": added,
        "уже были": len(parsed) - added,
        "ошибок": len(errors),
        "ошибки": errors[:20],
    }
