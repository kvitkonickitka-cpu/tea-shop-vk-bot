"""Загрузка ВМ и бота: процессор, память — раз в несколько секунд.

Зачем: на вопрос «сколько клиентов выдержит бот и когда начнутся
задержки» без этих цифр ответить нечем — метрики в Monitoring с Timeweb не
уходят. Сборщик живёт фоновой задачей в процессе бота и раз в
_SAMPLE_SECONDS читает /proc (в контейнере это цифры всей ВМ: /proc/stat и
/proc/meminfo не изолированы) и cgroup самого контейнера бота. Пульс раз в
минуту забирает сводку за минуту — среднее и пик — и кладёт её в свою
строку `ops_events` (столбец `data`). Пик внутри минуты важнее среднего:
ход модели короткий, и среднее за минуту его размазало бы.

Чтение файлов дешёвое (микросекунды); любая ошибка — пустая сводка, не сбой.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SAMPLE_SECONDS = 5
_CGROUP = Path("/sys/fs/cgroup")

_samples: list[dict] = []
_last_cpu: tuple[int, int, int] | None = None  # total, idle, steal
_last_bot: tuple[float, int] | None = None  # monotonic, usage_usec


def _host_cpu() -> tuple[float, float] | None:
    """Занятость процессора ВМ и доля steal (время, отнятое гипервизором), %."""
    global _last_cpu
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    steal = fields[7] if len(fields) > 7 else 0
    total = sum(fields[:8])
    previous, _last_cpu = _last_cpu, (total, idle, steal)
    if previous is None or total <= previous[0]:
        return None
    span = total - previous[0]
    return 100 * (1 - (idle - previous[1]) / span), 100 * (steal - previous[2]) / span


def _meminfo() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.split()[0])  # кБ
    return values


def _bot_cpu() -> float | None:
    """Процессор контейнера бота, % одного ядра."""
    global _last_bot
    stat = (_CGROUP / "cpu.stat").read_text()
    usage = int(next(line.split()[1] for line in stat.splitlines() if line.startswith("usage_usec")))
    now = time.monotonic()
    previous, _last_bot = _last_bot, (now, usage)
    if previous is None or now <= previous[0]:
        return None
    return 100 * (usage - previous[1]) / 1_000_000 / (now - previous[0])


def sample() -> dict:
    """Один замер. Чего не прочитали — того в замере нет."""
    point: dict = {}
    try:
        cpu = _host_cpu()
        if cpu:
            point["cpu"], point["steal"] = cpu
    except Exception:
        pass
    try:
        mem = _meminfo()
        point["mem_available_mb"] = mem["MemAvailable"] // 1024
        point["mem_total_mb"] = mem["MemTotal"] // 1024
        point["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024
    except Exception:
        pass
    try:
        point["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        pass
    try:
        bot_cpu = _bot_cpu()
        if bot_cpu is not None:
            point["bot_cpu"] = bot_cpu
        point["bot_mem_mb"] = int((_CGROUP / "memory.current").read_text()) // (1024 * 1024)
    except Exception:
        pass
    return point


def minute_summary() -> dict:
    """Сводка накопленных замеров (обычно за минуту) — и начать заново."""
    global _samples
    taken, _samples = _samples, []
    if not taken:
        return {}

    def values(key):
        return [s[key] for s in taken if key in s]

    summary: dict = {"samples": len(taken)}
    for key in ("cpu", "steal", "bot_cpu", "load1"):
        found = values(key)
        if found:
            summary[f"{key}_avg"] = round(sum(found) / len(found), 1)
            summary[f"{key}_max"] = round(max(found), 1)
    for key, pick in (("mem_available_mb", min), ("swap_used_mb", max), ("bot_mem_mb", max)):
        found = values(key)
        if found:
            summary[key] = pick(found)
    total = values("mem_total_mb")
    if total:
        summary["mem_total_mb"] = total[-1]
    return summary


async def run() -> None:
    sample()  # первый замер только запоминает счётчики процессора
    while True:
        await asyncio.sleep(_SAMPLE_SECONDS)
        try:
            point = sample()
            if point:
                _samples.append(point)
                # Пульс не пришёл (база лежит) — не копим бесконечно.
                del _samples[:-60]
        except Exception:
            logger.debug("Замер загрузки не удался", exc_info=True)
