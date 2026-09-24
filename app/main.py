import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.payments import router as payments_router
from app.api.vk import router as vk_router
from app.core import diagnostics
from app.core.config import settings
from app.core.database import init_models, is_available

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(health_router)
app.include_router(internal_router)
app.include_router(payments_router)
app.include_router(vk_router)


@app.on_event("startup")
async def on_startup() -> None:
    await init_models()

    if not is_available():
        # Две причины недоступности выглядят снаружи одинаково — молчание до
        # таймаута, — но чинятся по-разному, поэтому печатаем обе улики:
        # с какого локального адреса контейнер идёт к базе (адрес не из 10.x
        # означает, что подключения к VPC нет) и какой у него исходящий адрес
        # в интернете (по нему видно, какого префикса не хватает в правилах).
        diagnostics.log_route_to_database()
        await diagnostics.log_egress_ip()
