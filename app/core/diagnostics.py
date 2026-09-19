from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import settings

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


def log_route_to_database() -> None:
    """Пишет в лог, с какого локального адреса контейнер пошёл бы к базе.

    Отвечает на вопрос, действительно ли контейнер попал в VPC. Адрес из
    10.x означает, что он внутри сети и идёт к базе напрямую. Любой другой —
    что подключения к сети нет, и пакет к приватному адресу базы уходит в
    маршрут по умолчанию, где такие адреса не маршрутизируются: снаружи это
    выглядит неотличимо от закрытого файрвола.

    UDP-сокет здесь ничего не отправляет: connect() лишь заставляет ядро
    выбрать маршрут, а getsockname() показывает, что оно выбрало.
    """
    host = urlparse(settings.database_url).hostname
    if not host:
        logger.warning("В DATABASE_URL не разобрать хост — маршрут не проверить")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 5432))
        logger.warning("Маршрут до базы %s: локальный адрес %s", host, sock.getsockname()[0])
    except OSError:
        logger.warning("Маршрута до базы %s нет вовсе", host, exc_info=True)
    finally:
        sock.close()
