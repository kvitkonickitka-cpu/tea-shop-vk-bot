import hashlib

from fastapi import APIRouter, Request

from app.core.config import settings
from app.core.database import is_available

router = APIRouter(tags=["health"])


def _fingerprint(value: str) -> str:
    """Восемь символов sha256 — сверять, не показывая само значение."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _token_fingerprint() -> str:
    """Отпечаток служебного токена — чтобы сверять, не показывая сам токен.

    Токен вшит в ревизию при сборке, а лежит он ещё и в локальном `.env`, и в
    секретах GitHub. Разъехались — и все служебные эндпоинты отвечают
    `forbidden`, а понять, чей токен неправильный, нечем: сравнить значения
    напрямую нельзя, их нельзя ни показать, ни переслать. Восемь символов от
    sha256 для сверки достаточно, а обратно из них токен не достать.
    """
    token = (settings.internal_api_token or "").strip()
    if not token:
        return "не задан"
    return _fingerprint(token)


def _received_header(request: Request) -> str:
    value = request.headers.get("x-internal-token")
    if value is None:
        return "заголовок не пришёл"
    stripped = value.strip()
    if value != stripped:
        return f"{_fingerprint(stripped)} (по краям были лишние пробелы)"
    return _fingerprint(stripped)


@router.get("/health")
async def health_check(request: Request) -> dict[str, str]:
    # Кроме «жив», отвечаем какая ревизия крутится и видна ли база. Без
    # первого невозможно понять, доехал ли деплой: свежий эндпоинт отвечает
    # 404 и когда его нет в коде, и когда контейнер ещё на старой ревизии.
    return {
        "status": "ok",
        "revision": settings.app_revision or "не задана",
        "database": "ok" if is_available() else "резервный режим",
        "token": _token_fingerprint(),
        # Что дошло до приложения в заголовке. Отпечатки токена в ревизии и в
        # .env сошлись, а доступ всё равно закрывался — значит вопрос не к
        # значению, а к дороге: заголовок могли не донести или подправить по
        # пути. Своё значение вызывающий и так знает, так что его отпечаток
        # ему ничего не открывает.
        "header": _received_header(request),
    }
