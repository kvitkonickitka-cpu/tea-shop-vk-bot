import hmac
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.core import heartbeat
from app.core.config import settings
from app.messages import manager as manager_messages
from app.modules import events
from app.modules.catalog import vk_market
from app.modules.dialog import escalation_watch, telegram_client
from app.modules.delivery import ozon_catalog, ozon_client, ozon_quote
from app.modules.orders import cdek_watch, repository as orders_repository
from app.modules.payment import service as payment_service
from app.modules.payment import watch as payment_watch
from app.modules.payment import yookassa_client
from app.modules.queue import client as queue_client
from app.modules.reports import service as reports_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

# Сколько секунд тянуть каталог, когда выгрузку дёрнули руками. У контейнера
# на запрос 60, и кроме выгрузки в нём ничего не происходит.
_MANUAL_SYNC_SECONDS = 45

# Сколько секунд тик расписания считает своими. Меньше отведённых контейнеру
# 60: остаток нужен на то, чтобы задачи успели дописать результат в базу.
_TICK_BUDGET_SECONDS = 50


async def _authorized(request: Request, body: str | None = None) -> bool:
    # Адрес контейнера открыт всему интернету, так что служебные эндпоинты
    # защищены общим секретом. Пустой секрет закрывает их полностью: лучше
    # не работающие отчёты, чем эндпоинт, который любой может дёргать.
    # strip() с обеих сторон. Токен живёт в трёх местах — в локальном .env, в
    # секрете GitHub и в переменной окружения ревизии, — и в любое из них
    # легко заехать пробелу или переводу строки на краю. При этом curl
    # обрезает края значения заголовка сам: оба хранилища держат «токен с
    # пробелом», отпечатки сходятся, а до приложения доезжает токен без
    # пробела — и сравнение не совпадает. Искать такое глазами невозможно, а
    # осмысленного токена с пробелом по краям не бывает.
    expected = (settings.internal_api_token or "").strip()
    if not expected:
        return False

    provided = (
        request.headers.get("x-internal-token") or request.query_params.get("token", "")
    ).strip()
    # Сравниваем байтами: compare_digest на строках падает, если в них есть
    # что-то кроме ASCII, а токен приходит снаружи и может быть каким угодно.
    # Падение здесь означало бы 500 вместо честного «доступ закрыт».
    if provided and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return True

    # Тело — единственный способ передать токен, который работает всегда.
    # Таймер Yandex Cloud ни заголовков, ни пути задать не даёт, только
    # произвольную строку в поле «Данные». А заголовок `x-internal-token`,
    # как выяснилось 21.09.2026, до контейнера просто не доезжает: его
    # срезают по дороге, и приложение видит запрос без него. Поэтому ищем
    # токен в теле целиком: угадать 256 бит всё равно нельзя, а разбирать
    # чужой формат обёртки, который может измениться, — лишняя точка отказа.
    if body is None:
        # Starlette тело кэширует, так что повторное чтение безопасно.
        body = (await request.body()).decode("utf-8", errors="replace")
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
    """Всё, что делается по таймеру, а не в ответ на сообщение клиента.

    Порядок не случайный. Контейнеру на весь тик отведено 60 секунд, и если
    первая задача их выберет, до остальных дело не дойдёт вовсе — запрос
    просто убьют. Поэтому впереди идёт то, что дороже потерять.

    Сверка заказов первая: она решает, узнает ли менеджер о заказе. Каталог
    второй и берёт остаток бюджета. Отчёты по диалогам последние — они самые
    дорогие (обращение к Claude на каждый диалог) и легче всего переносят
    задержку: выжимка разговора нужна не в ту же минуту.
    """
    started = time.monotonic()
    result = {
        # Очередь уведомлений первой и дешёвой: в ней лежит то, что уже
        # обещано клиенту, — вопрос менеджеру, карточка оплаченного заказа.
        "manager_outbox": await _run_task(
            "Очередь уведомлений менеджеру", manager_messages.flush()
        ),
        # Платежи следом: потерянное уведомление означает оплаченный заказ,
        # который иначе не уедет никогда.
        "payments": await _run_task("Проверка платежей", payment_watch.check_pending()),
        "cdek_orders": await _run_task("Проверка заказов СДЭК", cdek_watch.check_pending_orders()),
    }

    # Вопросы без ответа — до каталога: проверка дешёвая (несколько строк в
    # базе), а каталог забирает весь остаток бюджета и до задач после него
    # дело может не дойти вовсе.
    result["open_questions"] = await _run_task(
        "Вопросы без ответа", escalation_watch.check_open_questions()
    )

    left = _TICK_BUDGET_SECONDS - (time.monotonic() - started)
    result["ozon_catalog"] = await _run_task(
        "Каталог Ozon", ozon_catalog.sync(budget_seconds=max(5, min(20, int(left))))
    )
    result["reports"] = await _run_task(
        "Отчёты по диалогам", reports_service.send_pending_reports()
    )
    result["undelivered"] = await _run_task(
        "Отчёт о недоставленном менеджеру", reports_service.report_undelivered()
    )
    # Отметка после всех задач: по ней видно, дошёл ли тик до конца или его
    # убили на середине — и firing ли триггер вообще.
    await heartbeat.note("расписание")
    return result


@router.post("/internal/reports/dialogs")
async def send_dialog_reports(request: Request):
    """Прогон задач по расписанию вручную — этим адресом удобно проверять."""
    if not await _authorized(request):
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
    if not await _authorized(request):
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


@router.post("/internal/orders/{order_id}/invoice")
async def issue_invoice(order_id: int, request: Request):
    """Выставить счёт по заказу и отдать ссылку менеджеру.

    Нужно, когда бот не смог: ЮKassa отказала, счёт истёк, клиент просит
    новую ссылку в переписке с человеком. Идёт тем же кодом, что и
    подтверждение в диалоге, — те же проверки полноты и та же запись
    попытки, — поэтому выставленный так счёт ничем не отличается от
    обычного: оплата по нему заведёт отправление сама.

        scripts/api.sh orders/12/invoice
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}

    try:
        payment, note = await payment_service.issue_for_order(order)
    except Exception as error:
        logger.exception("Не выставили счёт по заказу %s", order_id)
        return {"заказ": order_id, "error": f"{type(error).__name__}: {str(error)[:300]}"}

    if payment is None:
        logger.warning("Счёт по заказу %s не выставлен: %s", order_id, note)
        return {"заказ": order_id, "error": note}

    logger.info("Счёт по заказу %s выставлен вручную: платёж %s", order_id, payment.id)
    return {
        "заказ": order_id,
        "платёж": payment.id,
        "сумма": payment.amount,
        "статус": payment.status,
        "что дальше": note,
        "ссылка на оплату": payment.confirmation_url,
        "клиенту": f"Ссылку можно переслать клиенту: {payment.confirmation_url}",
    }


@router.post("/internal/catalog/vk")
async def probe_vk_market(request: Request):
    """Что лежит в витрине сообщества: разведка перед зеркалом ассортимента.

    Ассортимент бота пока живёт в `catalog.json` внутри образа, а в группе
    уже есть и цены, и описания, и признак доступности. Прежде чем делать
    зеркало, надо увидеть живые данные: заведены ли фасовки вариантами,
    включён ли учёт остатков, у всех ли товаров есть описание.

    Только чтение: `?limit=5` — сколько товаров разобрать подробно,
    `?raw=1` — добавить сырой ответ первого товара, как его отдал ВК.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    params = request.query_params
    try:
        limit = max(1, min(int(params.get("limit") or 5), 20))
    except ValueError:
        limit = 5

    try:
        result = await vk_market.probe(limit=limit)
    except Exception as error:
        # Без exception(): в трассировке запроса может оказаться токен
        # сообщества, а логи читает больше людей, чем стоило бы.
        safe = _hide_token(str(error))
        logger.error("Витрину ВК прочитать не вышло: %s", safe)
        return {"error": safe[:300]}

    if params.get("raw"):
        try:
            response = await vk_market.get_items(count=1)
            result["сырой товар"] = (response.get("items") or [None])[0]
        except Exception as error:
            result["сырой товар"] = {"error": _hide_token(str(error))[:300]}

    logger.info(
        "Витрина ВК: товаров %s, получено %s",
        result.get("всего товаров в магазине"),
        result.get("получено за один запрос"),
    )
    return result


@router.post("/internal/ozon/sync")
async def sync_ozon_catalog(request: Request):
    """Догрузить каталог пунктов Ozon вручную, не дожидаясь таймера."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    # Бюджет больше, чем у выгрузки по таймеру: там в том же тике работают
    # отчёты и проверка заказов, а здесь запрос делает только это.
    result = await ozon_catalog.sync(budget_seconds=_MANUAL_SYNC_SECONDS)
    result["всего в базе"] = await ozon_catalog.count()
    logger.info("Каталог Ozon: %s", result)
    return result


@router.post("/internal/ozon/stats")
async def ozon_stats(request: Request):
    """Что уже лежит в каталоге, без запуска выгрузки."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await ozon_catalog.stats()
    logger.info("Каталог Ozon, состав: %s", result)
    return result


@router.post("/internal/ozon/quote")
async def quote_ozon(request: Request):
    """Проверка подбора пункта и цены Ozon — тем же кодом, что и в диалоге.

    Каталог лежит в базе, база живёт во внутренней сети, и повторить подбор
    скриптом с ноутбука нельзя. Поэтому проверяем изнутри контейнера:
    `?city=Москва&point=Тверская&weight=400&value=1500`.
    """
    if not await _authorized(request):
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

    picked = await ozon_quote.points_for(
        city, hint, weight_grams=weight, declared_value=value
    )
    points = picked.points
    result = {
        "город": city,
        "искали": hint or "(только город)",
        "адрес сошёлся": picked.hint_matched if hint else "(адрес не называли)",
        "всего подходит в каталоге": picked.total,
        "взяли на проверку": picked.found,
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
    if not await _authorized(request):
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


@router.post("/internal/payments/me")
async def payments_account(request: Request):
    """Чей магазин отвечает на ключи ревизии. Ничего не создаёт.

    Проверять надо именно отсюда: ключи в `.env` на ноутбуке и ключи,
    вшитые в ревизию, — две независимые копии.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    if not yookassa_client.is_configured():
        return {"error": "YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы в ревизии"}

    try:
        data = await yookassa_client.account_info()
    except Exception as error:
        logger.exception("Не спросили у ЮKassa, чей это магазин")
        return {"error": str(error)[:300]}

    test = data.get("test")
    return {
        "магазин": data.get("account_id"),
        "контур": "тестовый" if test else ("БОЕВОЙ" if test is False else "не сказано"),
        "состояние": data.get("status"),
        "фискализация": data.get("fiscalization"),
        "способы оплаты": data.get("payment_methods"),
        "оплата включена в боте": settings.payments_enabled,
    }


@router.post("/internal/payments/check")
async def check_payments(request: Request):
    """Перечитать у ЮKassa платежи и чеки, не дожидаясь таймера."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await payment_watch.check_pending()
    logger.info("Проверка платежей: %s", result)
    return result


@router.post("/internal/cdek/check")
async def check_cdek_orders(request: Request):
    """Только сверка заказов с СДЭКом, без рассылки отчётов."""
    if not await _authorized(request):
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
    if not await _authorized(request, raw):
        # Громко, с именем: отказ на этом адресе означает, что расписание не
        # выполняется вообще, а выглядит это как тишина. Токен живёт в трёх
        # местах — .env, секрет GitHub и поле «Данные» самого триггера, — и
        # третью копию при смене забыли, отчего тик молча отваливался.
        logger.error(
            "Триггер постучался, но токен не подошёл: расписание не выполнено. "
            "Проверь поле «Данные» у таймерного триггера — там своя копия "
            "INTERNAL_API_TOKEN, и при смене её надо обновлять отдельно."
        )
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
