"""Заполненность диска ВМ с базой — через саму базу, без агента на ВМ.

Postgres умеет выполнить программу на своей стороне (`COPY … FROM
PROGRAM`), а его данные лежат на диске ВМ: `df` по каталогу данных
показывает ровно тот диск, который может кончиться. Так не нужно ставить на
ВМ агент, менять её сервисный аккаунт и платить за метрики.

Нужно, чтобы пользователь базы был суперпользователем или имел роль
`pg_execute_server_program`. В официальном образе postgres пользователь из
`POSTGRES_USER` — суперпользователь. Прав нет — функция вернёт размер базы
без диска, а отчёт напишет «нет данных».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import text

logger = logging.getLogger(__name__)

_SAFE_PATH = re.compile(r"[\w/.-]+")


@dataclass
class Disk:
    percent: int | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    db_bytes: int | None = None


def parse_df(lines: list[str]) -> tuple[int, int, int] | None:
    """Процент, всего и свободно (байты) из вывода `df -P`."""
    for line in reversed(lines):
        fields = line.split()
        if len(fields) >= 5 and fields[4].endswith("%") and fields[1].isdigit():
            return int(fields[4].rstrip("%")), int(fields[1]) * 1024, int(fields[3]) * 1024
    return None


async def disk() -> Disk:
    """Диск под базой и размер базы. Никогда не бросает."""
    from app.core import database

    result = Disk()
    if database._engine is None:
        return result
    try:
        async with database._engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            result.db_bytes = (
                await connection.execute(text("select pg_database_size(current_database())"))
            ).scalar_one()
            data_dir = (await connection.execute(text("show data_directory"))).scalar_one()
            if not _SAFE_PATH.fullmatch(data_dir or ""):
                return result
            # COPY FROM PROGRAM подготовить нельзя — только простым запросом
            # драйвера, мимо подготовленных выражений SQLAlchemy.
            raw = (await connection.get_raw_connection()).driver_connection
            await raw.execute("create temp table if not exists _ops_df (line text)")
            await raw.execute("truncate _ops_df")
            await raw.execute(f"copy _ops_df from program 'df -P {data_dir}'")
            lines = [row["line"] for row in await raw.fetch("select line from _ops_df")]
        parsed = parse_df(lines)
        if parsed:
            result.percent, result.total_bytes, result.free_bytes = parsed
    except Exception as error:
        logger.info("Диск ВМ не прочитался через базу: %s", type(error).__name__)
    return result


def gigabytes(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1024 ** 3:.1f}".replace(".", ",") + " ГБ"


def megabytes(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 1024 ** 2:.0f} МБ"
