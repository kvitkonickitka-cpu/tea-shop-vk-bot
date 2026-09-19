import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.vk import router as vk_router
from app.core import diagnostics
from app.core.config import settings
from app.core.database import init_models, is_available

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(health_router)
app.include_router(vk_router)


@app.on_event("startup")
async def on_startup() -> None:
    await init_models()

    if not is_available():
        # Самая частая причина недоступности базы — исходящий адрес контейнера
        # не попал в разрешённые в группе безопасности перед ней. Печатаем
        # адрес, чтобы было видно, какой префикс туда добавить.
        await diagnostics.log_egress_ip()
