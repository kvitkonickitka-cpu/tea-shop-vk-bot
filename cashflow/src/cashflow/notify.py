"""Уведомление об ошибке в Telegram. Если не настроено — молча пропускаем."""

from __future__ import annotations

import logging

import httpx

from .config import Config

log = logging.getLogger(__name__)


def send_error(cfg: Config, text: str) -> bool:
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return False
    try:
        with httpx.Client(timeout=15) as client:
            client.post(
                f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                json={"chat_id": cfg.telegram_chat_id, "text": text[:3500]},
            )
    except httpx.HTTPError as exc:
        log.warning("Не удалось отправить уведомление в Telegram: %s", exc)
        return False
    return True
