from fastapi import APIRouter

from app.core.config import settings
from app.core.database import is_available

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    # Кроме «жив», отвечаем какая ревизия крутится и видна ли база. Без
    # первого невозможно понять, доехал ли деплой: свежий эндпоинт отвечает
    # 404 и когда его нет в коде, и когда контейнер ещё на старой ревизии.
    return {
        "status": "ok",
        "revision": settings.app_revision or "не задана",
        "database": "ok" if is_available() else "резервный режим",
    }
