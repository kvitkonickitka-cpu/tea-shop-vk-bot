"""Псевдонимный ключ клиента: HMAC от VK ID с секретом из настроек.

Одна функция на всё, где клиента нужно назвать, не называя: аналитика
(`analytics.v_*`, столбец `client_key`) и хранилище меток персональных
данных (`pii_vault`). По ключу нельзя восстановить VK ID без секрета, а
секрет живёт только в переменных окружения.

VK ID — числа небольшого диапазона, их можно перебрать, поэтому без
секрета ключ не выдаём вовсе: пустой секрет — None, а не слабый ключ.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_warned = False


def client_key(peer_id: int | str | None) -> str | None:
    """Ключ клиента или None, если секрет не задан или клиента нет."""
    global _warned
    if peer_id in (None, ""):
        return None
    secret = settings.client_key_secret
    if not secret:
        if not _warned:
            logger.warning("CLIENT_KEY_SECRET не задан — ключи клиентов не считаются")
            _warned = True
        return None
    digest = hmac.new(secret.encode("utf-8"), str(int(peer_id)).encode("ascii"), hashlib.sha256)
    return digest.hexdigest()[:32]
