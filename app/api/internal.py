import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.modules import events
from app.modules.dialog import telegram_client
from app.modules.delivery import ozon_catalog, ozon_client, ozon_quote
from app.modules.orders import cdek_watch
from app.modules.queue import client as queue_client
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
    # Сравниваем байтами: compare_digest на строках падает, если в них есть
    # что-то кроме ASCII, а токен приходит снаружи и может быть каким угодно.
    # Падение здесь означало бы 500 вместо честного «доступ закрыт».
    if provided and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return True

    # Таймер Yandex Cloud ни заголовков, ни пути задать не даёт — только
    # произвольную строку в поле «Данные», и приходит она внутри его
    # собственной обёртки, форма которой нам не обещана. Поэтому ищем токен
    # в теле целиком: угадать 256 бит всё равно нельзя, а разбирать чужой
    # формат, который может измениться, — лишняя точка отказа.
    return bool(body) and expected in body


async def _run_task(name: str, coro) -> dict:
    """Одна задача расписания. Падение одной не должно ронять остальные.

    Раньше рассылка отчётов шла без защиты, и её ошибка обрывала весь тик —
    вместе с проверкой заказов и выгрузкой каталога, о которых в логах не
    оставалось ни строчки.
    """
    try:
        result = await coro
        logger.info("%s: %s", name, result)
        return result
    except Exception:
        logger.exception("%s — сорвалось", name)
        return {"failed": "исключение, см. лог"}


async def _run_scheduled() -> dict:
    """Всё, что делается по таймеру, а не в ответ на сообщение клиента."""
    return {
        "reports": await _run_task("Отчёты по диалогам", reports_service.send_pending_reports()),
        "cdek_orders": await _run_task("Проверка заказов СДЭК", cdek_watch.check_pending_orders()),
        "ozon_catalog": await _run_task("Каталог Ozon", ozon_catalog.sync()),
    }


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


@router.post("/internal/ozon/sync")
async def sync_ozon_catalog(request: Request):
    """Догрузить каталог пунктов Ozon вручную, не дожидаясь таймера."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await ozon_catalog.sync()
    result["всего в базе"] = await ozon_catalog.count()
    logger.info("Каталог Ozon: %s", result)
    return result


@router.post("/internal/ozon/quote")
async def quote_ozon(request: Request):
    """Проверка подбора пункта и цены Ozon — тем же кодом, что и в диалоге.

    Каталог лежит в базе, база живёт во внутренней сети, и повторить подбор
    скриптом с ноутбука нельзя. Поэтому проверяем изнутри контейнера:
    `?city=Москва&point=Тверская&weight=400&value=1500`.
    """
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    params = request.query_params
    city = (params.get("city") or "").strip()
    if not city:
        return {"error": "нужен параметр city"}

    hint = (params.get("point") or "").strip()
    weight = int(params.get("weight") or settings.cdek_default_package_weight_grams)
    value = float(params.get("value") or 1000)

    if not ozon_quote.is_ready():
        return {"error": "Ozon не настроен: нет ключей или OZON_SHIPMENT_METHOD_ID"}

    points, found, total = await ozon_quote.points_for(
        city, hint, weight_grams=weight, declared_value=value
    )
    result = {
        "город": city,
        "искали": hint or "(только город)",
        "всего подходит в каталоге": total,
        "взяли на проверку": found,
        "доступных пунктов": len(points),
    }

    # Считаем каждый доступный пункт, а не только единственный: в диалоге
    # цена называется по одному выбранному, а здесь важно ровно обратное —
    # видеть, расходятся ли цены внутри города. Пока непонятно, расходятся
    # ли, флоу обязан спрашивать пункт до цены.
    quotes = []
    for point in points:
        row = {"id": point.id, "адрес": point.address}
        try:
            quote = await ozon_quote.price_for(
                point.id, phone="", weight_grams=weight, declared_value=value
            )
        except Exception as error:
            logger.exception("Не посчитали доставку Ozon в пункт %s", point.id)
            row["ошибка расчёта"] = str(error)[:200]
        else:
            row["доставка"] = quote.delivery_cost
            row["страховка"] = quote.insurance_cost
            row["итого"] = quote.total
            row["дней"] = quote.days
        quotes.append(row)

    result["пункты"] = quotes
    return result


@router.post("/internal/ozon/posting")
async def ozon_posting(request: Request):
    """Состояние отправления Ozon, а с `cancel=1` — его отмена.

    Нужно для проверки боевого расчёта: заказ создаётся по-настоящему, и
    лишнее отправление надо уметь убрать, не заходя в кабинет.
    `?number=<номер отправления>&cancel=1`
    """
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    number = (request.query_params.get("number") or "").strip()
    if not number:
        return {"error": "нужен параметр number — номер отправления"}

    try:
        if request.query_params.get("cancel") in ("1", "true", "да"):
            logger.info("Отменяем отправление Ozon %s по служебному запросу", number)
            return {"отменено": number, "ответ": await ozon_client.cancel_posting(number)}
        return {"отправление": await ozon_client.posting_info(number)}
    except Exception as error:
        logger.exception("Не получилось с отправлением Ozon %s", number)
        return {"error": str(error)[:300]}


@router.post("/internal/cdek/check")
async def check_cdek_orders(request: Request):
    """Только сверка заказов с СДЭКом, без рассылки отчётов."""
    if not _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await cdek_watch.check_pending_orders()
    logger.info("Проверка заказов СДЭК: %s", result)
    return result


@router.post("/")
async def trigger_entrypoint(request: Request):
    """Общая точка входа для триггеров Yandex Cloud.

    В форме триггера нет поля пути: любой из них стучится в корень
    контейнера. Поэтому сюда приходят и таймер, и очередь, и различать их
    приходится по содержимому: у сообщения очереди есть тело, у таймера нет.

    Токен в обоих случаях приезжает внутри запроса — у таймера из поля
    «Данные», у очереди мы кладём его в сообщение сами, — так что проверка
    доступа одна на оба случая.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not _authorized(request, raw):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}

    vk_events = queue_client.extract_events(payload) if isinstance(payload, dict) else []
    if vk_events:
        handled = 0
        for event in vk_events:
            # Одно упавшее сообщение не должно уронить остальные из той же
            # пачки: триггер приносит их вместе.
            try:
                await events.process_event(event)
                handled += 1
            except Exception:
                logger.exception("Событие из очереди не обработалось: %s", event.get("event_id"))
        logger.info("Из очереди обработано событий: %s из %s", handled, len(vk_events))
        if handled < len(vk_events):
            # Отвечаем ошибкой, чтобы очередь принесла пачку ещё раз. Иначе
            # упавшее событие пропадёт совсем, а раньше его повторял сам VK.
            # Повторная обработка уже удавшихся отсеется дедупликацией по
            # event_id — она для того и живёт в базе.
            return Response(
                content=json.dumps({"queue_events": len(vk_events), "handled": handled}),
                media_type="application/json",
                status_code=500,
            )
        return {"queue_events": len(vk_events), "handled": handled}

    return await _run_scheduled()
