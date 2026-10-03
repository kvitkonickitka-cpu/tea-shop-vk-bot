"""Запись метрик Yandex Monitoring.

Авторизация — IAM-токен сервисного аккаунта ревизии из сервиса метаданных:
статических ключей нет, а токен живёт ровно столько, сколько ревизия.
Сервисному аккаунту нужна роль `monitoring.editor` — роли «только запись» у
Monitoring нет.

Метка с именем сервиса называется `api`, а не `service`: метку `service`
Monitoring ставит сам (`service=custom` в запросе записи).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)
_TIMEOUT_SECONDS = 5

_token = ""
_token_expires_at = 0.0


def is_configured() -> bool:
    return bool(settings.yc_folder_id)


async def _iam_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token
    response = await client.get(_METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    response.raise_for_status()
    data = response.json()
    _token = data["access_token"]
    # Минута запаса, чтобы токен не истёк посреди запроса.
    _token_expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
    return _token


def gauge(name: str, value: float, **labels: str) -> dict:
    metric = {"name": name, "type": "DGAUGE", "value": float(value)}
    if labels:
        metric["labels"] = labels
    return metric


async def write(metrics: list[dict]) -> bool:
    """Отправить пачку метрик. Никогда не бросает: False — не ушло."""
    if not is_configured() or not metrics:
        return False
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            token = await _iam_token(client)
            response = await client.post(
                f"{settings.monitoring_api_url}/write",
                params={"folderId": settings.yc_folder_id, "service": "custom"},
                headers={"Authorization": f"Bearer {token}"},
                json={"metrics": [{**metric, "ts": ts} for metric in metrics]},
            )
        if response.status_code >= 400:
            # Тело ответа Monitoring — про метрики, данных клиентов в нём нет.
            logger.warning(
                "Monitoring не принял метрики: HTTP %s %s",
                response.status_code, response.text[:300],
            )
            return False
        return True
    except Exception as error:
        logger.warning("Метрики не отправлены: %s", type(error).__name__)
        return False
