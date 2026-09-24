"""Отметки о том, что задача по расписанию действительно отработала.

Заведено после того, как выгрузка каталога двое суток стояла на месте, а
понять, виноват таймер или сама задача, было нечем: в логах пусто и там, где
задача не запускалась, и там, где её убили на середине. Отметка ставится
после выполнения и переживает перезапуск контейнера — по ней сразу видно,
firing ли триггер вообще.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_session_factory

logger = logging.getLogger(__name__)


class Heartbeat(Base):
    """Когда названная задача отработала в последний раз."""

    __tablename__ = "heartbeats"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Сколько раз отметилась с момента заведения таблицы: по одной дате не
    # отличить «идёт каждые пять минут» от «сработало один раз позавчера».
    runs: Mapped[int] = mapped_column(Integer, default=0)


async def note(name: str) -> None:
    """Отметить, что задача отработала. Молча не мешает работе, если не вышло."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return

    try:
        async with session_factory() as session:
            statement = insert(Heartbeat).values(
                name=name, last_run_at=datetime.now(timezone.utc), runs=1
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Heartbeat.name],
                    set_={
                        "last_run_at": statement.excluded.last_run_at,
                        "runs": Heartbeat.runs + 1,
                    },
                )
            )
            await session.commit()
    except Exception:
        # Отметка — диагностика, а не работа. Ронять из-за неё тик нельзя.
        logger.exception("Не записали отметку задачи %s", name)


async def last_run(name: str) -> datetime | None:
    """Когда названная задача отметилась в последний раз."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return None

    try:
        async with session_factory() as session:
            row = await session.get(Heartbeat, name)
    except Exception:
        logger.exception("Не прочитали отметку задачи %s", name)
        return None

    if row is None:
        return None
    moment = row.last_run_at
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


async def describe() -> dict:
    """Что и когда отрабатывало — для /health."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return {}

    from sqlalchemy import select

    try:
        async with session_factory() as session:
            rows = (await session.execute(select(Heartbeat))).scalars().all()
    except Exception:
        logger.exception("Не прочитали отметки задач")
        return {}

    return {
        row.name: f"{row.last_run_at.strftime('%d.%m %H:%M')} UTC, всего раз: {row.runs}"
        for row in rows
    }
