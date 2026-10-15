import asyncio
import logging

from fastapi import FastAPI, Request

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.packing import router as packing_router
from app.api.payments import router as payments_router
from app.api.vk import router as vk_router
from app.core import diagnostics, logs
from app.core.config import settings
from app.core.database import init_models, is_available
from app.modules.catalog import sheet as catalog_sheet
from app.modules.ops import journal as ops_journal

# Строка JSON на запись: Cloud Logging разбирает её на поля, а форматтер
# заодно маскирует телефоны, почты и токены (app/core/logs.py).
logs.setup(logging.INFO)
# httpx на уровне INFO пишет полный адрес каждого запроса, а в адресе бывают
# секреты: токен бота в пути Telegram API, client_secret в запросе токена
# СДЭКа. Они оказывались в логах контейнера открытым текстом.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(title=settings.app_name, debug=settings.debug)

# Вебхук ВК ждёт ответа считаные секунды — журнал мониторинга после него
# не сбрасываем, его подберёт следующий запрос или пульс.
_NO_FLUSH_PATHS = ("/vk/callback", "/health")


@app.middleware("http")
async def flush_ops_journal(request: Request, call_next):
    response = await call_next(request)
    # Сбой мониторинга не должен испортить ответ: flush не бросает, и у него
    # свой короткий таймаут.
    if ops_journal.pending() and request.url.path not in _NO_FLUSH_PATHS:
        await ops_journal.flush()
    return response



@app.middleware("http")
async def fresh_catalog(request: Request, call_next):
    # Каталог из Google Таблицы кладёт в базу тик расписания; здесь каждый
    # контейнер подтягивает его в память — отпечаток сверяет раз в 5 секунд. Путь
    # сообщения клиента в Google не ходит: у вебхука ВК около восьми секунд.
    await catalog_sheet.ensure_fresh()
    return await call_next(request)


app.include_router(health_router)
app.include_router(internal_router)
app.include_router(packing_router)
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

    from app.modules.queue import client as queue_client

    if queue_client.uses_local():
        from app.modules.queue import local as local_queue

        # Обработчик своей очереди событий ВК (вместо Yandex Message Queue).
        app.state.local_queue = asyncio.create_task(local_queue.run())

    if settings.ops_assistant_enabled and settings.telegram_bot_token and settings.telegram_ops_chat_id:
        from app.modules.ops.assistant import poller

        # Ссылка на задачу — в состоянии приложения: иначе сборщик мусора
        # может прибить фоновую задачу, на которую никто не ссылается.
        app.state.ops_assistant = asyncio.create_task(poller.run())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for name in ("ops_assistant", "local_queue"):
        task = getattr(app.state, name, None)
        if task is not None:
            task.cancel()
