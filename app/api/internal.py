import hmac
import logging

from fastapi import APIRouter, Request, Response

from app.core.config import settings
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


async def _run_reports() -> dict:
    result = await reports_service.send_pending_reports()
    logger.info("Отчёты по диалогам: %s", result)
    return result


@router.post("/internal/reports/dialogs")
async def send_dialog_reports(request: Request):
    """Рассылка отчётов вручную — этим адресом удобно проверять."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await _run_reports()


@router.post("/")
async def timer_entrypoint(request: Request):
    """Точка входа для таймера Yandex Cloud.

    В форме триггера нет поля пути: он всегда стучится в корень контейнера.
    Токен кладётся в поле «Данные» и приезжает где-то внутри тела запроса.
    """
    body = (await request.body()).decode("utf-8", errors="replace")
    if not _authorized(request, body):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await _run_reports()
