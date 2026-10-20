"""Способы оплаты, реально включённые в магазине ЮKassa (C2).

Список — из `GET /v3/me` (`payment_methods`): bank_card, sbp, sberbank (SberPay),
tinkoff_bank (T-Pay), yoo_money, alfa_pay… Кэш на 6 часов: способы включают в
кабинете редко, а счёт выставляется в пути ответа клиенту. Настройка
`ENABLED_PAYMENT_METHODS` (через запятую) перекрывает ответ ЮKassa — на случай,
если способ включён, но предлагать его не хотим. Не узнали — способы не
называем вовсе: бот не обещает того, чего может не быть.
"""

from __future__ import annotations

import logging
import time

from app.core import flags
from app.core.config import settings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 6 * 3600
_cache: tuple[float, list[str]] | None = None


def forget_cache() -> None:
    global _cache
    _cache = None


async def enabled() -> list[str]:
    global _cache
    configured = [m.strip() for m in (settings.enabled_payment_methods or "").split(",") if m.strip()]
    if configured:
        return configured
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    from app.modules.payment import yookassa_client

    try:
        info = await yookassa_client.account_info()
        methods = [str(m) for m in info.get("payment_methods") or []]
    except Exception as error:
        logger.warning("Способы оплаты ЮKassa не узнали: %s", type(error).__name__)
        methods = _cache[1] if _cache else []
    _cache = (time.monotonic(), methods)
    return methods


async def trust_line(peer_id: int) -> str:
    """Строка под счётом: ЮKassa, способы, чек по 54-ФЗ, реквизиты ИП. Флаг выключен — пусто."""
    if not await flags.enabled_for(flags.PAYMENT_TRUST, peer_id):
        return ""
    from app.messages import templates

    return templates.payment_trust_line(await enabled(), settings.requisites_url)


async def prompt_note(peer_id: int) -> str:
    """Для модели: какие способы оплаты называть (только включённые)."""
    if not await flags.enabled_for(flags.PAYMENT_TRUST, peer_id):
        return ""
    from app.messages import templates

    phrase = templates.payment_methods_phrase(await enabled())
    if not phrase:
        return "Способы оплаты не называй: какие включены в магазине, сейчас неизвестно."
    return (f"Оплатить можно {phrase} — на странице ЮKassa способ выбирает сам клиент. Другие способы "
            "(наличные, перевод на карту, оплата при получении) не предлагай и не обещай.")
