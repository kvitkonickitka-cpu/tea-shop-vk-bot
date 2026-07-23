from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.modules.dialog import service
from app.modules.orders import service as orders_service

router = APIRouter(tags=["vk"])


@router.post("/vk/callback")
async def vk_callback(request: Request) -> Response:
    body = await request.json()

    if body.get("secret") != settings.vk_secret_key:
        return Response(content="ok", media_type="text/plain", status_code=403)

    event_type = body.get("type")

    if event_type == "confirmation":
        return Response(content=settings.vk_confirmation_token, media_type="text/plain")

    event_id = body.get("event_id", "")
    if event_id and service.is_duplicate(event_id):
        return Response(content="ok", media_type="text/plain")

    # Обрабатываем синхронно, до ответа "ok" — на serverless-платформах
    # (Yandex Cloud) фоновые задачи после ответа не гарантированно
    # довыполняются, контейнер может быть заморожен раньше времени.
    if event_type == "message_new":
        message = body.get("object", {}).get("message", {})
        await service.handle_message_new(message)
    elif event_type == "message_reply":
        # У message_new object вложен под ключом "message", у message_reply
        # по документации VK — это сам объект сообщения; на случай если VK
        # пришлёт другой формат, подстрахуемся обоими вариантами.
        reply_object = body.get("object", {})
        message = reply_object.get("message", reply_object)
        await service.handle_message_reply(message)
    elif event_type == "market_order_new":
        order_event = body.get("object", {})
        await orders_service.handle_new_order(order_event)

    return Response(content="ok", media_type="text/plain")
