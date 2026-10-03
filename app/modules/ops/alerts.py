"""Сообщения в Ops-чат о том, что действительно сломалось.

Алерты Monitoring на метриках оказались шумными: без клиентов у метрик нет
точек, и Monitoring всю ночь сообщал «No points in metric» — почти 600
сообщений, ни одного по делу. Поэтому о проблемах, которые бот видит сам,
пишет сам бот — и только когда проблема есть:

- **сбой внешнего сервиса** — `details.py`, с операцией, кодом и заказом;
- **медленные ответы** — p95 за окно выше порога, и ответов не меньше
  `ops_slow_min_turns`: один долгий ответ — ещё не тенденция;
- **база не отвечает** — два пульса подряд (около двух минут), затем одно
  сообщение и одно «снова отвечает». Отметка живёт в памяти экземпляра:
  записать её в базу, которая не отвечает, нельзя;
- **диск ВМ с базой** — 80 % и 90 %, проверка тиком раз в 5 минут.

Чего бот о себе сказать не может — что он сам лежит. Для этого остаётся
единственный алерт Monitoring «бот не работает» (нет пульса).

Повторы ограничены паузой: отметка в `heartbeats` с именем `ops-…`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app.core import heartbeat
from app.core.config import settings
from app.modules.ops import host

logger = logging.getLogger(__name__)

SLOW_COOLDOWN = timedelta(hours=1)
DISK_COOLDOWN = {"alarm": timedelta(hours=6), "warn": timedelta(hours=24)}
_DB_CONFIRM_SECONDS = 110
_DB_REPEAT_SECONDS = 30 * 60

# Когда база перестала отвечать и когда об этом сказали — в памяти процесса.
_db_down_since: float | None = None
_db_noticed_at: float | None = None


async def _send(text: str) -> None:
    from app.modules.dialog import telegram_client

    await telegram_client.send_message(text, chat_id=settings.telegram_ops_chat_id)


async def _due(name: str, cooldown: timedelta, now: datetime) -> bool:
    last = await heartbeat.last_run(name)
    return last is None or now - last >= cooldown


def _seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} с"


async def check_slow(stats: dict | None, now: datetime | None = None) -> bool:
    """Медленные ответы клиентам. True — сообщение ушло."""
    if not settings.telegram_ops_chat_id or not stats:
        return False
    if stats["turns"] < settings.ops_slow_min_turns or (stats["p95"] or 0) <= settings.ops_slow_seconds:
        return False
    now = now or datetime.now(timezone.utc)
    if not await _due("ops-медленно", SLOW_COOLDOWN, now):
        return False
    llm = stats.get("llm_p95") or 0
    hint = (
        "Время уходит на Claude — тормозит модель или её прокси."
        if llm >= 0.6 * stats["p95"]
        else "Claude отвечает быстро — тормозит наш код или перевозчики: в логах строки «ход peer_id=…»."
    )
    await _send(
        f"🐢 <b>Медленные ответы</b>: p95 {_seconds(stats['p95'])} за {settings.ops_window_minutes} мин "
        f"по {stats['turns']} ответам, из них Claude — {_seconds(llm)}.\n{hint}"
    )
    await heartbeat.note("ops-медленно")
    return True


async def check_db(db_up: bool) -> str | None:
    """База не отвечает боту — сказать один раз, и один раз — что ожила."""
    global _db_down_since, _db_noticed_at
    if not settings.telegram_ops_chat_id:
        return None
    moment = time.time()
    if db_up:
        _db_down_since, noticed = None, _db_noticed_at
        _db_noticed_at = None
        if noticed is not None:
            await _send("🟢 <b>База снова отвечает</b> боту.")
            return "up"
        return None
    if _db_down_since is None:
        _db_down_since = moment
    if moment - _db_down_since < _DB_CONFIRM_SECONDS:
        return None
    if _db_noticed_at is not None and moment - _db_noticed_at < _DB_REPEAT_SECONDS:
        return None
    minutes = int((moment - _db_down_since) // 60)
    await _send(
        f"🔴 <b>База не отвечает боту</b> уже {minutes} мин.\n"
        "Бот отвечает клиентам, но история и заказы живут только в памяти.\n"
        "Проверь ВМ с базой (Compute Cloud), на ней: docker ps, "
        "docker logs --tail 50 teashop-postgres. Таймаут — фильтр, refused — Postgres не запущен."
    )
    _db_noticed_at = moment
    return "down"


async def check_disk(now: datetime | None = None) -> dict:
    """Диск ВМ с базой: 80 % — предупреждение, 90 % — тревога."""
    disk = await host.disk()
    result = {"percent": disk.percent, "free": host.gigabytes(disk.free_bytes)}
    if disk.percent is None or not settings.telegram_ops_chat_id:
        return result
    if disk.percent >= settings.ops_disk_alarm_percent:
        level, mark = "alarm", "🔴"
    elif disk.percent >= settings.ops_disk_warn_percent:
        level, mark = "warn", "⚠️"
    else:
        return result
    now = now or datetime.now(timezone.utc)
    name = f"ops-диск-{level}"
    if not await _due(name, DISK_COOLDOWN[level], now):
        return result
    await _send(
        f"{mark} <b>Диск ВМ с базой заполнен на {disk.percent} %</b> — свободно "
        f"{host.gigabytes(disk.free_bytes)} из {host.gigabytes(disk.total_bytes)}, база — "
        f"{host.megabytes(disk.db_bytes)}.\nЧто делать: docs/monitoring.md, раздел «Диск»."
    )
    await heartbeat.note(name)
    result["sent"] = level
    return result


def reset() -> None:
    """Для тестов: забыть состояние базы в памяти."""
    global _db_down_since, _db_noticed_at
    _db_down_since = _db_noticed_at = None
