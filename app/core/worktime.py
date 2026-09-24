"""Время по Москве: рабочие часы менеджера и тихие часы для рассылок.

Смещение задано числом, а не `ZoneInfo("Europe/Moscow")`: в образе может не
оказаться базы часовых поясов, и тогда падал бы весь тик расписания. Москва
живёт на UTC+3 без переходов с 2014 года, так что фиксированное смещение
здесь не упрощение, а факт.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.core.config import settings

MSK = timezone(timedelta(hours=3), "MSK")


def now_msk() -> datetime:
    return datetime.now(MSK)


def to_msk(moment: datetime) -> datetime:
    """Момент в московском времени. Наивный считаем UTC — так его пишет база."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MSK)


def hhmm(moment: datetime) -> str:
    """Часы и минуты по Москве — для текста клиенту."""
    return to_msk(moment).strftime("%H:%M")


def is_quiet(moment: datetime | None = None) -> bool:
    """Тихие часы: ночью напоминания не рассылаем.

    Окно переходит через полночь (22:00–09:00), поэтому сравнение не
    «больше и меньше», а «больше или меньше».
    """
    local = to_msk(moment or now_msk())
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end
    hour = local.hour
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def quiet_until(moment: datetime | None = None) -> datetime:
    """Когда закончатся тихие часы — момент в московском времени."""
    local = to_msk(moment or now_msk())
    end = settings.quiet_hours_end
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def is_working(moment: datetime | None = None) -> bool:
    """Рабочие часы менеджера. Дни недели все: магазин работает без выходных."""
    local = to_msk(moment or now_msk())
    return settings.manager_work_hours_start <= local.hour < settings.manager_work_hours_end


def working_minutes_between(start: datetime, end: datetime) -> int:
    """Сколько рабочих минут прошло между двумя моментами.

    Считаем по часам, а не по календарю: вопрос, заданный в 23:40, ждёт
    ответа не всю ночь, а с открытия. Шаг — минута; интервалы у нас
    измеряются часами, и точности хватает с запасом.
    """
    left = to_msk(start)
    right = to_msk(end)
    if right <= left:
        return 0

    minutes = 0
    cursor = left.replace(second=0, microsecond=0)
    step = timedelta(minutes=1)
    # Потолок на случай очень старой записи: считать год по минуте незачем.
    limit = 60 * 24 * 14
    while cursor < right and minutes <= limit:
        if is_working(cursor):
            minutes += 1
        cursor += step
    return minutes


def working_day_phrase(moment: datetime | None = None) -> str:
    """«сегодня» или «в ближайший рабочий день» — по времени суток."""
    return "сегодня" if is_working(moment) else "в ближайший рабочий день"


def parse_hour(value: int, default: int) -> int:
    return value if 0 <= value <= 23 else default


__all__ = [
    "MSK",
    "hhmm",
    "is_quiet",
    "is_working",
    "now_msk",
    "quiet_until",
    "to_msk",
    "working_day_phrase",
    "working_minutes_between",
]
