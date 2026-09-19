import hmac
import logging

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.modules.reports import service as reports_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


def _authorized(request: Request) -> bool:
    # Адрес контейнера открыт всему интернету, так что служебные эндпоинты
    # защищены общим секретом. Пустой секрет закрывает их полностью: лучше
    # не работающие отчёты, чем эндпоинт, который любой может дёргать.
    expected = settings.internal_api_token
    if not expected:
        return False

    provided = request.headers.get("x-internal-token") or request.query_params.get("token", "")
    return hmac.compare_digest(provided, expected)


@router.post("/internal/reports/dialogs")
async def send_dialog_reports(request: Request):
    """Рассылает мини-отчёты по замолчавшим диалогам. Дёргается по таймеру."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    result = await reports_service.send_pending_reports()
    logger.info("Отчёты по диалогам: %s", result)
    return result
