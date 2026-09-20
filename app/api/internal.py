import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.modules.dialog import telegram_client
from app.modules.orders import cdek_watch
from app.modules.reports import service as reports_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


def _authorized(request: Request, body: str = "") -> bool:
    # Адрес контейнера открыт всему интернету, так что служебные эндпоинты
    # защищены общим секретом. Пустой секрет закрывает их полностью: лучше
    # не работающие отчёты, чем эндпоинт, который любой может дёргать.
    expected = settings.internal_api_token
    if not expected:
        return False

    provided = request.headers.get("x-internal-token") or request.query_params.get("token", "")
    if provided and hmac.compare_digest(provided, expected):
        return True

    # Таймер Yandex Cloud ни заголовков, ни пути задать не даёт — только
    # произвольную строку в поле «Данные», и приходит она внутри его
    # собственной обёртки, форма которой нам не обещана. Поэтому ищем токен
    # в теле целиком: угадать 256 бит всё равно нельзя, а разбирать чужой
    # формат, который может измениться, — лишняя точка отказа.
    return bool(body) and expected in body


async def _run_scheduled() -> dict:
    """Всё, что делается по таймеру, а не в ответ на сообщение клиента."""
    reports = await reports_service.send_pending_reports()
    logger.info("Отчёты по диалогам: %s", reports)

    # Проверка заказов не должна падать вместе с отчётами и наоборот: это
    # независимые задачи, просто ходят по одному расписанию.
    try:
        orders = await cdek_watch.check_pending_orders()
        logger.info("Проверка заказов СДЭК: %s", orders)
    except Exception:
        logger.exception("Проверка заказов СДЭК сорвалась")
        orders = {"failed": "исключение, см. лог"}

    return {"reports": reports, "cdek_orders": orders}


@router.post("/internal/reports/dialogs")
async def send_dialog_reports(request: Request):
    """Прогон задач по расписанию вручную — этим адресом удобно проверять."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await _run_scheduled()


def _hide_token(text: str) -> str:
    """Убрать токен бота из текста ошибки.

    httpx кладёт в сообщение об ошибке полный URL, а он у Telegram вида
    `/bot<токен>/sendMessage`. Отдавать это наружу нельзя даже через
    защищённый токеном эндпоинт: ответ легко переслать или вставить в чат.
    """
    token = settings.telegram_bot_token
    return text.replace(token, "<токен скрыт>") if token else text


@router.post("/internal/telegram/ping")
async def telegram_ping(request: Request):
    """Проверка связи: пишет тестовое сообщение в чат заказов."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    chat_id = settings.telegram_orders_chat_id or None
    where = chat_id or "чат менеджера (TELEGRAM_ORDERS_CHAT_ID не задан)"
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    text = (
        "✅ <b>Проверка связи</b>\n"
        "Бот пишет в этот чат. Сюда будут приходить карточки новых заказов "
        "и предупреждения о проблемах с регистрацией в СДЭКе.\n"
        f"Отправлено: {now}"
    )

    try:
        await telegram_client.send_message(text, chat_id=chat_id)
    except Exception as error:
        # Без exception(): в трассировке может оказаться токен бота, а логи
        # читает больше людей, чем стоило бы.
        safe = _hide_token(str(error))
        logger.error("Проверка связи с чатом заказов не прошла: %s", safe)
        return {"chat": where, "sent": False, "error": safe[:300]}

    logger.info("Проверка связи: сообщение ушло в %s", where)
    return {"chat": where, "sent": True}


@router.post("/internal/cdek/check")
async def check_cdek_orders(request: Request):
    """Только сверка заказов с СДЭКом, без рассылки отчётов."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await cdek_watch.check_pending_orders()
    logger.info("Проверка заказов СДЭК: %s", result)
    return result


@router.post("/")
async def timer_entrypoint(request: Request):
    """Точка входа для таймера Yandex Cloud.

    В форме триггера нет поля пути: он всегда стучится в корень контейнера.
    Токен кладётся в поле «Данные» и приезжает где-то внутри тела запроса.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    if not _authorized(request, body):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await _run_scheduled()
