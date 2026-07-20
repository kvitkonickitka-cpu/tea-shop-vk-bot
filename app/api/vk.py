from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.core.config import settings
from app.modules.dialog import service
from app.modules.orders import service as orders_service

router = APIRouter(tags=["vk"])


@router.post("/vk/callback")
async def vk_callback(request: Request, background_tasks: BackgroundTasks) -> Response:
    body = await request.json()

    if body.get("secret") != settings.vk_secret_key:
        return Response(content="ok", media_type="text/plain", status_code=403)

    event_type = body.get("type")

    if event_type == "confirmation":
        return Response(content=settings.vk_confirmation_token, media_type="text/plain")

    event_id = body.get("event_id", "")
    if event_id and service.is_duplicate(event_id):
        return Response(content="ok", media_type="text/plain")

    if event_type == "message_new":
        message = body.get("object", {}).get("message", {})
        background_tasks.add_task(service.handle_message_new, message)
    elif event_type == "market_order_new":
        order_event = body.get("object", {})
        background_tasks.add_task(orders_service.handle_new_order, order_event)

    return Response(content="ok", media_type="text/plain")
