import logging

from fastapi import APIRouter, Request, Response

from app.modules.payment import webhook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])


@router.post("/payments/yookassa")
async def yookassa_notification(request: Request):
    """Уведомление ЮKassa о смене статуса платежа.

    Адрес прописывается в кабинете: Интеграция — HTTP-уведомления. Требования
    ЮKassa к нему — HTTPS и порт 443 или 8443; у контейнера так и есть.

    Токеном не защищён: ЮKassa передать его не может. Подлинность
    проверяется иначе — состояние платежа спрашивается у ЮKassa, а не
    берётся из тела (см. `app/modules/payment/webhook.py`).
    """
    try:
        body = await request.json()
    except ValueError:
        # Неразбираемое тело повторять бессмысленно — отвечаем успехом,
        # иначе ЮKassa будет присылать это сутки.
        logger.warning("Уведомление ЮKassa не разобралось как JSON")
        return {"ignored": "тело не разобралось"}

    try:
        return await webhook.handle(body if isinstance(body, dict) else {})
    except Exception:
        # Отвечаем ошибкой намеренно: ЮKassa повторит уведомление, и
        # оплаченный заказ не потеряется из-за нашей поломки.
        logger.exception("Не обработали уведомление ЮKassa")
        return Response(
            content='{"error":"retry"}', media_type="application/json", status_code=500
        )
