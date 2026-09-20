"""Отправка события в Yandex Message Queue.

Зачем очередь: VK ждёт ответа на вебхук около восьми секунд, а обработка —
Claude, СДЭК, скоро ЮKassa и другие доставки — в этот бюджет уже не
помещается. С очередью вебхук отвечает «принял» за доли секунды, а
разбирает событие отдельный вызов, над которым секундомера нет.

Yandex Message Queue совместима с SQS, поэтому говорим с ней через boto3.
Клиент создаётся один раз и живёт в контейнере: создавать его на каждое
сообщение — лишние десятки миллисекунд там, где мы считаем каждую.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

_YMQ_ENDPOINT = "https://message-queue.api.cloud.yandex.net"
_client = None


def is_configured() -> bool:
    return bool(
        settings.ymq_queue_url
        and settings.ymq_access_key_id
        and settings.ymq_secret_access_key
    )


def _get_client():
    global _client
    if _client is None:
        import boto3  # локально: тянуть его при старте незачем, если очередь не нужна

        _client = boto3.client(
            "sqs",
            endpoint_url=_YMQ_ENDPOINT,
            region_name="ru-central1",
            aws_access_key_id=settings.ymq_access_key_id,
            aws_secret_access_key=settings.ymq_secret_access_key,
        )
    return _client


def _send_sync(body: str) -> str:
    response = _get_client().send_message(
        QueueUrl=settings.ymq_queue_url,
        MessageBody=body,
    )
    return response.get("MessageId", "")


async def enqueue(vk_event: dict) -> str:
    """Положить событие ВК в очередь. Возвращает идентификатор сообщения.

    Токен служебных эндпоинтов кладём внутрь сообщения: триггер приносит его
    обратно в теле запроса, и тот же самый проверяющий код узнаёт своего.
    Заводить для очереди отдельный способ авторизации значило бы держать два.
    """
    # Секрет сообщества в очереди ни к чему: его проверяет вебхук, дальше по
    # пути он никому не нужен, а лежать в хранилище и в логах будет.
    event = {k: v for k, v in vk_event.items() if k != "secret"}
    body = json.dumps(
        {"token": settings.internal_api_token, "vk_event": event},
        ensure_ascii=False,
    )
    # boto3 синхронный, поэтому уводим в поток: блокировать цикл событий на
    # время сетевого запроса нельзя — рядом обрабатываются другие сообщения.
    return await asyncio.to_thread(_send_sync, body)


def extract_events(payload: dict) -> list[dict]:
    """Вытащить события ВК из того, что принёс триггер очереди.

    Форма обёртки у Yandex Cloud своя и нигде нам не обещана, поэтому не
    разбираем её по именам полей, а ищем сообщения с нашим содержимым.
    Заодно это отличает сообщения очереди от срабатывания таймера, который
    стучится в тот же самый адрес: у него никакого `body` нет.
    """
    events = []
    for message in payload.get("messages") or []:
        raw = ((message.get("details") or {}).get("message") or {}).get("body")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Сообщение очереди не разобралось как JSON, пропускаем")
            continue
        event = parsed.get("vk_event")
        if event:
            events.append(event)
    return events
