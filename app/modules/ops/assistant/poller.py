"""Длинный опрос Telegram для ассистента Ops.

Вебхук не подходит: с ВМ Timeweb Telegram доступен только через прокси на
Aeza, а входящие от Telegram на российский адрес могут резаться. Длинный
опрос идёт тем же путём, что и все сообщения бота, и не требует нового
открытого адреса. Процесс uvicorn один, поэтому опрос не задваивается; в
секунды деплоя, пока жив старый контейнер, Telegram отвечает 409 — ждём.

Опрос живёт фоновой задачей приложения и ничего не ломает: любая ошибка —
пауза и новая попытка, обработка вопроса — в своей задаче.
"""

from __future__ import annotations

import asyncio
import logging

from app.modules.dialog import telegram_client
from app.modules.ops.assistant import service

logger = logging.getLogger(__name__)

_WAIT_SECONDS = 25
_RETRY_SECONDS = 15
_CONFLICT_SECONDS = 30


async def run() -> None:
    me: dict | None = None
    offset: int | None = None
    failures = 0
    while True:
        try:
            if me is None:
                me = await telegram_client.get_me()
                logger.info("Ассистент Ops слушает чат Ops как @%s", me.get("username"))
            updates = await telegram_client.get_updates(offset, wait_seconds=_WAIT_SECONDS)
            failures = 0
        except asyncio.CancelledError:
            raise
        except telegram_client.TelegramConflict:
            logger.warning("Ассистент Ops: обновления бота забирает кто-то ещё (409), ждём %s с", _CONFLICT_SECONDS)
            await asyncio.sleep(_CONFLICT_SECONDS)
            continue
        except Exception as error:
            failures += 1
            # Telegram или прокси лежат: тревогу об этом и так пришлют, в лог —
            # не каждую минуту, а первую ошибку и потом каждую двадцатую.
            if failures == 1 or failures % 20 == 0:
                logger.warning("Ассистент Ops: опрос Telegram не удался (%s раз подряд): %s",
                               failures, str(error)[:200])
            await asyncio.sleep(_RETRY_SECONDS)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                service.handle_update(update, me)
            except Exception:
                logger.exception("Ассистент Ops: обновление %s не разобралось", update.get("update_id"))
