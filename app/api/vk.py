import logging

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.modules import events
from app.modules.dialog import telegram_client
from app.modules.queue import client as queue_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vk"])

_OK = Response(content="ok", media_type="text/plain")


@router.post("/vk/callback")
async def vk_callback(request: Request) -> Response:
    body = await request.json()

    if body.get("secret") != settings.vk_secret_key:
        return Response(content="ok", media_type="text/plain", status_code=403)

    if body.get("type") == "confirmation":
        return Response(content=settings.vk_confirmation_token, media_type="text/plain")

    # VK ждёт ответа около восьми секунд, а обработка в них уже не помещается:
    # Claude, СДЭК, дальше ЮKassa и другие доставки. Поэтому кладём событие в
    # очередь и отвечаем сразу — разберёт его отдельный вызов, без секундомера.
    if queue_client.is_configured():
        try:
            message_id = await queue_client.enqueue(body)
            logger.info(
                "Событие %s поставлено в очередь: %s", body.get("event_id", ""), message_id
            )
            return _OK
        except Exception:
            # Очередь недоступна — лучше обработать на месте и, возможно,
            # не уложиться в таймаут, чем потерять сообщение клиента совсем.
            logger.exception("Не удалось поставить событие в очередь, обрабатываем на месте")

    # На месте — значит под секундомером VK: Telegram здесь ждём коротко.
    with telegram_client.hurry():
        await events.process_event(body)
    return _OK
