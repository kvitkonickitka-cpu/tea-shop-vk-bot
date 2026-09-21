import hashlib

from fastapi import APIRouter

from app.core.config import settings
from app.core.database import is_available

router = APIRouter(tags=["health"])


def _token_fingerprint() -> str:
    """Отпечаток служебного токена — чтобы сверять, не показывая сам токен.

    Токен вшит в ревизию при сборке, а лежит он ещё и в локальном `.env`, и в
    секретах GitHub. Разъехались — и все служебные эндпоинты отвечают
    `forbidden`, а понять, чей токен неправильный, нечем: сравнить значения
    напрямую нельзя, их нельзя ни показать, ни переслать. Восемь символов от
    sha256 для сверки достаточно, а обратно из них токен не достать.
    """
    if not settings.internal_api_token:
        return "не задан"
    digest = hashlib.sha256(settings.internal_api_token.encode("utf-8")).hexdigest()
    return digest[:8]


@router.get("/health")
async def health_check() -> dict[str, str]:
    # Кроме «жив», отвечаем какая ревизия крутится и видна ли база. Без
    # первого невозможно понять, доехал ли деплой: свежий эндпоинт отвечает
    # 404 и когда его нет в коде, и когда контейнер ещё на старой ревизии.
    return {
        "status": "ok",
        "revision": settings.app_revision or "не задана",
        "database": "ok" if is_available() else "резервный режим",
        "token": _token_fingerprint(),
    }
