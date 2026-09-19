from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_EGRESS_IP_SERVICE = "https://api.ipify.org"


async def log_egress_ip() -> None:
    """Пишет в лог исходящий IP контейнера.

    Нужен, когда база стоит за группой безопасности со списком разрешённых
    префиксов: у serverless-контейнера нет постоянного исходящего адреса, он
    меняется при перезапуске сам по себе. По этой строчке в логе видно, какого
    префикса не хватает в правилах, и не приходится ради диагностики временно
    открывать порт базы наружу.
    """
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(_EGRESS_IP_SERVICE)
            response.raise_for_status()
    except Exception:
        logger.warning("Не удалось определить исходящий IP контейнера", exc_info=True)
        return

    logger.warning("Исходящий IP контейнера: %s", response.text.strip())
