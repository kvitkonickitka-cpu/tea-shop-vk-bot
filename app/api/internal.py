import hmac
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.core import heartbeat, worktime
from app.core.config import settings
from app.messages import manager as manager_messages, templates
from app.modules import events
from app.modules.analytics import references as analytics_references
from app.modules.ops import anthropic_costs
from app.modules.analytics import service as analytics_service
from app.modules.catalog import sheet as catalog_sheet, vk_market
from app.modules.marking import packing, pool as marking_pool
from app.modules.ops import alerts as ops_alerts, journal as ops_journal, pulse as ops_pulse, report as ops_report
from app.modules.dialog import escalation_watch, inbound, telegram_client
from app.modules.delivery import ozon_catalog, ozon_client, ozon_quote
from app.modules.orders import (
    cancellation,
    cdek_watch,
    delivery_events,
    delivery_watch,
    draft_nudge,
    order_chat,
    repository as orders_repository,
    retention,
)
from app.modules.payment import returns, service as payment_service, settlement
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


async def _carrier_check() -> dict:
    from app.modules.delivery import acts

    return await acts.check()


async def _daily(name: str, job, *, after_hour_msk: int = 10) -> dict:
    """Задача раз в день после часа по Москве; отметка — в heartbeats (видна в отчёте Ops)."""
    now = datetime.now(timezone.utc)
    local = worktime.to_msk(now)
    if local.hour < after_hour_msk:
        return {"skipped": "рано"}
    last = await heartbeat.last_run(name)
    if last is not None and worktime.to_msk(last).date() == local.date():
        return {"skipped": "сегодня уже"}
    result = await job()
    await heartbeat.note(name)
    return result


async def _retention_summary() -> dict:
    result = await retention.check()
    decisions = result.pop("decisions", [])
    result["отложено"] = sum(1 for d in decisions if d.outcome == "blocked")
    return result


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
        # Судьба посылок после регистрации: передали, вручили, вернули. От
        # вручения зависит закрывающий чек, поэтому раньше каталога.
        "deliveries": await _run_task("Статусы доставки", delivery_watch.check_deliveries()),
        # Закрывающие чеки: статус у ЮKassa, повтор после сбоя сети.
        "settlement_receipts": await _run_task("Закрывающие чеки", settlement.check()),
    }

    # Вопросы без ответа — до каталога: проверка дешёвая (несколько строк в
    # базе), а каталог забирает весь остаток бюджета и до задач после него
    # дело может не дойти вовсе.
    result["open_questions"] = await _run_task(
        "Вопросы без ответа", escalation_watch.check_open_questions()
    )

    # Сообщения клиентов, которые никто не взял: хозяин хода умер между
    # ответом и передачей остатка. Дёшево и важнее всего остального.
    result["inbound"] = await _run_task("Сообщения без ответа", inbound.rescue_stale())

    # Своя очередь: разобранные события старше суток — удалить (там тексты
    # сообщений), и заодно видно, не копится ли неразобранное.
    from app.modules.queue import local as local_queue

    if local_queue.is_enabled():
        result["queue"] = await _run_task("Очередь событий", _queue_tick(local_queue))

    # Геопозиции клиентов старше GEO_RETENTION_HOURS — удалить: после выбора
    # пункта они не нужны, а хранить точку на карте дольше незачем.
    from app import privacy

    result["geo_forgotten"] = await _run_task("Геопозиции: удаление старых", privacy.forget_old_geo())

    # Брошенные черновики: пара запросов к базе, и тоже до каталога Ozon.
    result["draft_nudges"] = await _run_task(
        "Брошенные черновики", draft_nudge.check_drafts()
    )
    # Аналитика: клиенты, ключи, пометка тестовых данных — пара запросов.
    result["analytics"] = await _run_task("Аналитика: клиенты и тесты", analytics_service.sync())
    # Повторные касания после вручения: оценка, «Повторить», второй шанс,
    # реактивация — общим фильтром и по одному на клиента.
    result["retention"] = await _run_task("Повторные касания", _retention_summary())
    # Брошенный черновик или счёт — касание через ~20 ч; посылка в пункте — напоминания (C3, C4).
    from app.modules.orders import abandoned, pickup_reminders

    result["abandoned"] = await _run_task("Брошенные заказы: касание", abandoned.check())
    result["pickup"] = await _run_task("Посылка в пункте: напоминания", pickup_reminders.check())
    # Награда пригласившему (если включена) — после вручения заказа друга и срока возврата.
    from app.modules.referral import service as referral

    result["referral_rewards"] = await _run_task("Рефералка: награды", referral.grant_rewards())

    # Ассортимент из Google Таблицы — до каталога Ozon: один запрос, а
    # каталог Ozon забирает весь остаток бюджета.
    result["catalog_sheet"] = await _run_task("Каталог из таблицы", catalog_sheet.refresh())
    # Правки каталога в базе: цена, продажа, архив — в Ops-чат.
    from app.modules.catalog import store as catalog_store

    result["catalog_changes"] = await _run_task("Каталог: правки в Ops", catalog_store.notify_changes())
    # Коды введены в оборот — собранные заказы Ozon подтвердить к отгрузке.
    from app.modules.marking import circulation

    result["codes_introduced"] = await _run_task("Коды ЧЗ: подтверждение Ozon после ввода",
                                                 circulation.approve_introduced())
    # Справочники (себестоимость, настройки) — та же таблица, раз в 15 минут.
    result["references"] = await _run_task("Справочники из таблицы", analytics_references.sync())
    # Счёт Anthropic по дням — раз в час, админ-ключом.
    result["anthropic_costs"] = {"skipped": "Anthropic отключён"}
    # Реестр ЮKassa и сверка выплат с выпиской — раз в день после выписки 07:00.
    from app.modules.payment import registry as yk_registry

    result["yk_registry"] = await _run_task("Реестр ЮKassa", yk_registry.sync())
    # Регламенты: критичные контроли — сразу, сводка и утренний список — в 10:00,
    # реклама за вчера — в 11:00, закрытие месяца — 1-го (повтор 3-го), налоги — за 7 дней и за день.
    from app.modules.ops import controls

    result["controls"] = await _run_task("Контроли", controls.check_controls())
    result["morning"] = await _run_task("Утренний список", controls.morning_list())
    result["month_close"] = await _run_task("Закрытие месяца", controls.month_close())
    result["taxes"] = await _run_task("Налоговый календарь", controls.tax_reminders())
    # Факт доставки против котировки (акты и API) — раз в день, после реестра.
    result["carrier_check"] = await _run_task("Сверка доставки с актами", _daily("сверка доставки", _carrier_check))
    # Сверка учёта бота со счётом за прошлый месяц — с третьего числа, раз в месяц.
    result["anthropic_reconcile"] = {"skipped": "Anthropic отключён"}

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
    # Сутки бота для Ops-чата — раз в день после девяти утра по Москве.
    result["ops_report"] = await _run_task("Отчёт для Ops", ops_report.send())
    result["disk"] = await _run_task("Диск ВМ с базой", ops_alerts.check_disk())
    # Выгрузка Т-Банка и копия finance: cashflow сам в Telegram не пишет.
    from app.modules.ops import cashflow_watch

    result["cashflow"] = await _run_task("Cashflow: выгрузка и копия", cashflow_watch.check())
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


_MANUAL_EVENTS = {
    "handed-over": delivery_events.HANDED_OVER,
    "at-pickup": delivery_events.AT_PICKUP,
    "delivered": delivery_events.DELIVERED,
    "not-delivered": delivery_events.NOT_DELIVERED,
}


# Пути перечислены явно, а не шаблоном `{event}`: шаблон перехватывал бы и
# остальные команды заказа, объявленные после него.
@router.post("/internal/orders/{order_id}/handed-over")
@router.post("/internal/orders/{order_id}/at-pickup")
@router.post("/internal/analytics/sync")
async def analytics_sync(request: Request):
    """Клиенты и тестовые данные для аналитики.

        scripts/api.sh analytics/sync            что будет помечено тестовым
        scripts/api.sh 'analytics/sync?apply=1'  пометить сейчас (тик делает это сам)
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await analytics_service.sync(apply=request.query_params.get("apply") in ("1", "true", "да"))


@router.post("/internal/pii/migrate")
async def pii_migrate(request: Request):
    """Перенос накопленной истории на метки персональных данных.

        scripts/api.sh pii/migrate              пробный прогон: сколько и каких меток, примеры с масками
        scripts/api.sh 'pii/migrate?apply=1'    применить; перед записью — копия таблицы истории
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.privacy import migrate

    return await migrate.run(apply=request.query_params.get("apply") in ("1", "true", "да"))


@router.post("/internal/consents/{vk_user_id}")
async def consents_journal(vk_user_id: int, request: Request):
    """Журнал согласий покупателя — по его запросу или для проверки.

        scripts/api.sh consents/123456
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.consent import service as consent

    return {
        "vk_user_id": vk_user_id,
        "действует": {"pd_processing": await consent.has_pd(vk_user_id),
                      "marketing": await consent.marketing_allowed(vk_user_id)},
        "редакция": settings.pd_consent_version,
        "журнал": await consent.journal(vk_user_id),
    }


@router.post("/internal/consents/{vk_user_id}/revoke")
async def consents_revoke(vk_user_id: int, request: Request):
    """Отзыв, пришедший не через бота: письмом или менеджеру.

        scripts/api.sh 'consents/123456/revoke?source=email'     (или source=manager)
        scripts/api.sh 'consents/123456/revoke?source=email&only=marketing'   только рассылка
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.messages import marketing
    from app.modules.consent import service as consent
    from app.modules.orders import state

    source = request.query_params.get("source") or consent.REVOKE_MANAGER
    if source not in (consent.REVOKE_EMAIL, consent.REVOKE_MANAGER):
        return Response(content="source — email или manager", media_type="text/plain", status_code=400)
    only_marketing = request.query_params.get("only") == "marketing"
    kinds = (consent.MARKETING,) if only_marketing else (consent.PD, consent.MARKETING)
    await consent.revoke(vk_user_id, kinds, source=source, method="manual")
    await marketing.opt_out(vk_user_id, record_consent=False)
    if not only_marketing:
        await state.clear_draft(vk_user_id)
    return {"отозвано": list(kinds), "источник": source, "журнал": await consent.journal(vk_user_id)}


@router.post("/internal/clients/{vk_user_id}/anonymize")
async def clients_anonymize(vk_user_id: int, request: Request):
    """Обезличить покупателя после отзыва согласия. Без apply — пробный прогон.

        scripts/api.sh 'clients/123456/anonymize'            что изменится
        scripts/api.sh 'clients/123456/anonymize?apply=1'    применить
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.consent import erase

    flag = lambda name: request.query_params.get(name) in ("1", "true", "да")  # noqa: E731
    return await erase.anonymize(vk_user_id, apply=flag("apply"), force=flag("force"))


@router.post("/internal/retention/check")
async def retention_check(request: Request):
    """Повторные касания для одного клиента — что ушло бы и почему нет.

        scripts/api.sh 'retention/check?peer_id=123'                  что сейчас
        scripts/api.sh 'retention/check?peer_id=123&days=21'          как через 21 день
        scripts/api.sh 'retention/check?peer_id=123&days=21&send=1'   и отправить

    `days` сдвигает «сейчас» вперёд: так цепочку оценка → «Повторить» →
    второй шанс → реактивация можно пройти на своём аккаунте без ожидания.
    Отправка со сдвигом пишет в журнал сдвинутое время — паузы между
    касаниями считаются от него, как если бы дни и правда прошли.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from datetime import timedelta

    params = request.query_params
    try:
        peer_id = int(params.get("peer_id", ""))
    except ValueError:
        return {"error": "нужен peer_id клиента"}
    days = float(params.get("days") or 0)
    send = params.get("send") in ("1", "true", "да")
    now = datetime.now(timezone.utc) + timedelta(days=days)
    result = await retention.check(now, peer_id=peer_id, dry=not send)
    blocker = await retention.blocker(peer_id, now)
    return {
        "сейчас (МСК)": worktime.to_msk(now).strftime("%d.%m.%Y %H:%M"),
        "общий фильтр": blocker or "пропускает",
        "решения": [
            f"{retention.NAMES[d.kind]} по заказу №{d.order_id}: {d.outcome}"
            + (f" — {d.reason}" if d.reason else "")
            for d in result.get("decisions", [])
        ] or [result.get("skipped") or "созревших касаний нет"],
        "отправлено": result.get("sent", 0),
    }


@router.post("/internal/retention/delivery-stats")
async def retention_delivery_stats(request: Request):
    """Как часто доходит «вручено» — от него зависит вся цепочка касаний.

        scripts/api.sh retention/delivery-stats

    По Ozon и СДЭКу: сколько оплаченных заказов отправлено, сколько вручено и
    не вручено, сколько едут дольше 14 дней без «вручено» и сколько так и
    не переданы перевозчику.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await retention.delivery_stats()


@router.post("/internal/orders/{order_id}/delivered")
@router.post("/internal/orders/{order_id}/not-delivered")
async def mark_delivery_event(order_id: int, request: Request):
    """Отметить событие доставки руками, когда перевозчик его не отдаёт.

        scripts/api.sh orders/12/handed-over     посылку сдали перевозчику
        scripts/api.sh orders/12/at-pickup       посылка ждёт в пункте выдачи
        scripts/api.sh orders/12/delivered       посылку вручили
        scripts/api.sh orders/12/not-delivered   не вручили, едет обратно

    Идёт тем же кодом, что и опрос перевозчика: отметка ставится один раз,
    и если опрос успел раньше, команда просто скажет, что событие уже было.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)

    kind = _MANUAL_EVENTS[request.url.path.rstrip("/").rsplit("/", 1)[-1]]

    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}

    recorded = await delivery_events.record(
        order_id, kind, source=delivery_events.SOURCE_MANUAL
    )
    if recorded is None:
        return {
            "заказ": order_id,
            "событие": kind,
            "действий": "нет — событие уже отмечено (или заказ уже вручён / не вручён)",
            "передан": str(order.handed_over_at or "—"),
            "вручён": str(order.delivered_at or "—"),
            "не вручён": str(order.not_delivered_at or "—"),
        }
    logger.info("Заказ %s: событие %s отмечено вручную", order_id, kind)
    return {"заказ": order_id, "событие": kind, "отмечено": True}


@router.post("/internal/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request):
    """Отменить заказ руками — клиенту ничего не пишет.

        scripts/api.sh orders/12/cancel            неоплаченный
        scripts/api.sh "orders/12/cancel?paid=1"   и оплаченный (тестовый)

    Оплаченный отменяется только с `paid=1`: деньги сами не вернутся и
    отправление у перевозчика не отменится.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    paid_too = request.query_params.get("paid") in ("1", "true", "да")
    return await cancellation.cancel_by_manager(order_id, paid_too=paid_too)


@router.post("/internal/orders/{order_id}/settlement-receipt")
async def settlement_receipt(order_id: int, request: Request):
    """Отправить закрывающий чек по вручённому заказу — после исправления.

        scripts/api.sh orders/12/settlement-receipt

    Идёт тем же кодом, что и событие «вручено»: те же проверки (оплачен,
    есть почта, собран с кодами, сумма сходится), тот же ключ
    идемпотентности. Если чек уже есть — второй не создаётся.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}
    if order.delivered_at is None:
        return {
            "заказ": order_id,
            "error": "заказ ещё не вручён — чек уходит при вручении. Если "
                     f"вручён, но перевозчик молчит: scripts/api.sh orders/{order_id}/delivered",
        }
    return {"заказ": order_id, **(await settlement.issue(order))}


@router.post("/internal/orders/{order_id}/refund-with-codes")
async def refund_with_codes(order_id: int, request: Request):
    """Полный возврат заказа, проданного с кодами маркировки.

        scripts/api.sh orders/12/refund-with-codes

    Только после закрывающего чека: в чек возврата уходят те же коды, что в
    нём, и коды получают статус «возвращён». До закрывающего чека возврат —
    как раньше, кнопкой в кабинете ЮKassa. Возврат части заказа — вручную.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}
    return {"заказ": order_id, **(await returns.refund_with_codes(order))}


@router.post("/internal/orders/{order_id}/pack-link")
async def pack_link(order_id: int, request: Request):
    """Свежая ссылка на страницу сборки — и она же в чат заказов.

        scripts/api.sh orders/12/pack-link

    Нужна, когда срок ссылки из карточки вышел или карточка ушла без неё
    (не был задан PUBLIC_BASE_URL). В чат — чтобы открыть с телефона.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    order = await orders_repository.by_id(order_id)
    if order is None:
        return {"error": f"заказа №{order_id} нет в базе"}

    base = settings.public_base_url
    if not base:
        # Запрос пришёл на адрес контейнера — он и есть публичный адрес.
        # Схему поправляем: шлюз передаёт запрос внутрь по http.
        base = str(request.base_url).rstrip("/")
        if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
            base = "https://" + base[len("http://"):]
    made = packing.pack_url(order_id, base)
    if made is None:
        return {"error": "ссылку не собрать: не задан INTERNAL_API_TOKEN"}
    url, expires = made
    await order_chat.send(order, templates.manager_pack_link(order_id, url, expires))
    return {"заказ": order_id, "ссылка": url, "действует до (МСК)": f"{worktime.to_msk(expires):%d.%m %H:%M}"}


@router.post("/internal/codes/import")
async def import_marking_codes(request: Request):
    """Загрузить пул кодов маркировки из выгрузки СУЗ «Честного знака».

        scripts/api.sh codes/import codes.csv

    TXT (код на строку) или CSV. Повторная загрузка того же файла ничего не
    портит: уже известные коды пропускаются. После первой загрузки при
    сборке принимаются только коды из пула.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not await _authorized(request, raw):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    token = (settings.internal_api_token or "").strip()
    return await marking_pool.import_codes(raw, skip=token)


@router.post("/internal/delivery/check")
async def check_deliveries(request: Request):
    """Спросить перевозчиков о посылках в пути, не дожидаясь таймера."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await delivery_watch.check_deliveries()
    logger.info("Статусы доставки: %s", result)
    return result


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


@router.post("/internal/anthropic/costs")
async def anthropic_costs_sync(request: Request):
    """Прочитать счёт Anthropic сейчас, не дожидаясь часа.

        scripts/api.sh anthropic/costs

    Отвечает, за какие дни прочитано, сколько строк и итог в долларах — или
    почему не прочитано. Ключ в ответ не попадает.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return {"skipped": "Anthropic отключён; новые расходы — OpenAI"}


@router.post("/internal/references/sync")
async def references_sync(request: Request):
    """Прочитать справочники из таблицы сейчас, не дожидаясь 15 минут.

        scripts/api.sh references/sync

    Отвечает, сколько строк применено по каждой вкладке и какие строки
    пропущены и почему.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await analytics_references.sync(force=True)


@router.post("/internal/catalog/import-from-sheet")
async def catalog_import_from_sheet(request: Request):
    """Перенести каталог из Google Таблицы в базу — разово.

        scripts/api.sh catalog/import-from-sheet            # только отчёт
        scripts/api.sh 'catalog/import-from-sheet?apply=1'  # записать новые артикулы

    Таблица только читается. Уже заведённые в базе артикулы не трогаются.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.catalog import sheet_import

    return await sheet_import.run(apply=request.query_params.get("apply") in ("1", "true", "да"))


@router.post("/internal/catalog/sheet")
async def catalog_from_sheet(request: Request):
    """Прочитать Google Таблицу каталога сейчас, не дожидаясь тика.

        scripts/api.sh catalog/sheet

    Отвечает, применилась ли таблица, и если нет — какие строки с ошибками.
    Заодно показывает, что бот продаёт после чтения.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    result = await catalog_sheet.refresh(force=True)
    from app.modules.catalog import service as catalog_service

    result["бот продаёт"] = [
        f"{item['name']} — {item['price']} руб."
        + ("" if item.get("in_stock", True) else " (нет в наличии)")
        + (f", GTIN {item['gtin']}" if item.get("gtin") else "")
        for item in catalog_service.load_items()
    ]
    result["источник"] = (
        "Google Таблица" if catalog_sheet.current_items() is not None else "catalog.json в образе"
    )
    # Каким способом читается таблица — для проверки после переключения
    # SHEETS_AUTH_MODE. Почта сервисного аккаунта не секрет, ключа здесь нет.
    if catalog_sheet._mode() == catalog_sheet.SERVICE_ACCOUNT:
        from app.core import google_sheets

        account = google_sheets.client().account
        result["доступ"] = "сервисный аккаунт" + (f" {account}" if account else "")
    else:
        result["доступ"] = "публичная ссылка (CSV)"
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
    `?city=Москва&point=Тверская&weight=400&value=1500`, а `&phone=8921…` —
    чтобы проверить, примет ли Ozon расчёт с таким номером.
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
                point.id, phone=(params.get("phone") or "").strip(),
                weight_grams=weight, declared_value=value,
            )
        except Exception as error:
            logger.exception("Не посчитали доставку Ozon в пункт %s", point.id)
            # Тип обязателен: у таймаута httpx текст пустой, и в выводе
            # оставалось «ошибка расчёта: ""» без единой подсказки.
            row["ошибка расчёта"] = f"{type(error).__name__}: {error}"[:200]
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
@router.post("/internal/yookassa/me")
async def payments_account(request: Request):
    """Чей магазин отвечает на ключи ревизии и что в нём включено. Ничего не создаёт.

    Проверять надо именно отсюда: ключи в `.env` на ноутбуке и ключи,
    вшитые в ревизию, — две независимые копии.

        scripts/api.sh yookassa/me

    Способы оплаты и фискализацию показываем ровно так, как их отдал
    `GET /v3/me`: если поля в ответе нет, так и пишем, без догадок.
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

    missing = "ЮKassa в ответе /me это поле не отдала"
    test = data.get("test")
    fiscalization = data.get("fiscalization")
    if fiscalization is None and "fiscalization_enabled" in data:
        fiscalization = {"enabled": data.get("fiscalization_enabled")}
    return {
        "магазин": data.get("account_id"),
        "контур": "тестовый" if test else ("БОЕВОЙ" if test is False else "не сказано"),
        "состояние": data.get("status", missing),
        "фискализация": fiscalization if fiscalization is not None else missing,
        "способы оплаты": data.get("payment_methods", missing),
        "оплата включена в боте": settings.payments_enabled,
        # Способ оплаты бот не фиксирует: confirmation=redirect без
        # payment_method_data — клиент выбирает его сам на странице ЮKassa.
        "способ выбирает клиент": True,
        "поля ответа": sorted(data.keys()),
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



async def _queue_tick(local_queue) -> dict:
    removed = await local_queue.cleanup()
    stats = await local_queue.stats()
    if (stats["oldest_seconds"] or 0) > 300:
        logger.error("Очередь: самое старое событие ждёт %s с, ждут %s — обработчик стоит?",
                     stats["oldest_seconds"], stats["waiting"])
    return {"removed": removed, **stats}

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
    from app.core import database

    # Таймеры и очередь приходят в любую копию контейнера — копия, оставшаяся
    # без базы, через них же и переподключается (не чаще раза в 30 с).
    await database.recover()

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

    # Минутный таймер мониторинга: в его поле «Данные» — `ops-pulse` и токен.
    # Тик расписания ему запускать нельзя: тот идёт до 50 секунд и тянет
    # внешние сервисы, а пульс должен быть лёгким и каждую минуту.
    if ops_pulse.MARK in raw:
        return await ops_pulse.run()

    return await _run_scheduled()


@router.post("/internal/ops/pulse")
async def ops_pulse_now(request: Request):
    """Пульс вручную: что ушло бы в Monitoring прямо сейчас.

        scripts/api.sh ops/pulse
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    return await ops_pulse.run()


@router.post("/internal/ops/report")
async def ops_report_now(request: Request):
    """Ежедневный отчёт в Ops-чат прямо сейчас — без отметки «отправлен».

        scripts/api.sh ops/report
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    try:
        return await ops_report.send(force=True)
    except Exception as error:
        logger.exception("Отчёт для Ops не отправился")
        return {"sent": False, "error": _hide_token(f"{type(error).__name__}: {error}")[:300]}


@router.post("/internal/ops/emulate")
async def ops_emulate(request: Request):
    """Проверка алертов: записать поддельные сбои или медленные ответы.

    Внешние сервисы при этом не вызываются вовсе — запись идёт прямо в
    журнал с операцией «эмуляция», которую ежедневный отчёт не считает.
    Следом запускается пульс, так что метрика уходит сразу.

        scripts/api.sh 'ops/emulate?api=cdek&count=3&kind=http_5xx'
        scripts/api.sh 'ops/emulate?latency=45&count=5'
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    params = request.query_params
    try:
        count = max(1, min(int(params.get("count") or 1), 20))
        latency = float(params["latency"]) if params.get("latency") else None
    except ValueError:
        return {"error": "count и latency — числа"}
    api = params.get("api")
    if latency is not None:
        for _ in range(count):
            ops_journal.note_turn(latency, latency * 0.8, operation=ops_journal.EMULATION)
    elif api in ops_journal.APIS:
        kind = params.get("kind") or "http_5xx"
        status = {"http_5xx": 503, "http_4xx": 400, "auth": 401}.get(kind)
        error = RuntimeError(f"Эмуляция сбоя: HTTP {status}" if status else f"Эмуляция сбоя: {kind}")
        for _ in range(count):
            ops_journal.note_error(api, ops_journal.EMULATION, error, 0.0)
    else:
        return {"error": f"нужен latency=секунды или api=одно из {', '.join(ops_journal.APIS)}"}
    return {"emulated": count, "pulse": await ops_pulse.run()}


@router.post("/internal/flags")
async def feature_flags(request: Request):
    """Фича-флаги: список или переключение — то же, что /flag в чате Ops.

        scripts/api.sh flags
        scripts/api.sh 'flags?name=<флаг>&mode=allowlist'
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.core import flags

    name, mode = request.query_params.get("name"), request.query_params.get("mode")
    if name and mode:
        try:
            previous = await flags.set_mode(name, mode.casefold(), by="api")
        except ValueError as error:
            return {"error": str(error)}
        return {"флаг": name, "было": previous, "стало": mode.casefold()}
    return {"флаги": await flags.describe()}


@router.post("/internal/clients/{peer_id}/own")
async def mark_own_client(peer_id: int, request: Request):
    """Метка «свои» — то же, что /own в чате Ops. `?off=1` — снять."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.dialog import sessions

    off = request.query_params.get("off") == "1"
    found = await sessions.mark_own(peer_id, by="api", own=not off)
    return {"peer_id": peer_id, "найден": found, "свои": found and not off}


@router.post("/internal/smoke/dialog")
async def smoke_dialog(request: Request):
    """Проверка в бою: сессии, события воронки и расход Claude тестового аккаунта.

        scripts/api.sh 'smoke/dialog?peer_id=<тестовый VK ID>'

    Только для TEST_VK_IDS и без текстов: источники, отметки, коды событий, суммы.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    try:
        peer_id = int(request.query_params.get("peer_id") or 0)
    except ValueError:
        return {"error": "peer_id — число"}
    if peer_id not in await analytics_service.test_peer_ids():
        return {"error": "только для тестовых аккаунтов (TEST_VK_IDS)"}
    from sqlalchemy import text as sql

    from app.core.client_key import client_key
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        def rows(result):
            return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r._mapping.items()} for r in result]

        client = rows(await session.execute(sql(
            "select first_source, ref, own_at is not null as own, is_test from clients where peer_id = :p"), {"p": peer_id}))
        dialogs = rows(await session.execute(sql(
            "select id, started_at, last_message_at, source, close_reason, consult_started_at, consult_qualified_at "
            "from dialog_sessions where peer_id = :p order by id desc limit 5"), {"p": peer_id}))
        events = rows(await session.execute(sql(
            "select created_at, event, dialog_id, order_id, source from funnel_events where peer_id = :p "
            "order by id desc limit 30"), {"p": peer_id}))
        usage = rows(await session.execute(sql(
            "select at, operation, dialog_id, order_id, stage, purpose, cost_usd from llm_usage "
            "where client_key = :k order by id desc limit 15"), {"k": client_key(peer_id)}))
    return {"клиент": client, "сессии": dialogs, "события": events, "claude": usage}


@router.post("/internal/yookassa/registry")
async def yookassa_registry(request: Request):
    """Реестр ЮKassa сейчас: платежи и возвраты (только GET) и сверка выплат с выпиской.

        scripts/api.sh yookassa/registry          за последние 14 дней (первый раз — 120)
        scripts/api.sh 'yookassa/registry?days=60'
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.payment import registry as yk_registry

    try:
        days = int(request.query_params["days"]) if request.query_params.get("days") else None
    except ValueError:
        return {"error": "days — число"}
    try:
        return await yk_registry.sync(force=True, days=days)
    except Exception as error:
        logger.exception("Реестр ЮKassa руками не собрался")
        return {"error": f"{type(error).__name__}: {error}"[:300]}


@router.post("/internal/kudir")
async def kudir_export(request: Request):
    """КУДиР за период строками (для проверки; файл — /kudir в чате Ops).

        scripts/api.sh 'kudir?period=2026-10'
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.core import worktime
    from app.modules.payment import kudir

    try:
        start, end = kudir.parse_period((request.query_params.get("period") or "").split(),
                                        worktime.to_msk(datetime.now(timezone.utc)).date())
    except ValueError as error:
        return {"error": str(error)}
    records = await kudir.rows(start, end)
    return {"период": f"{start} — {end}", "правило": kudir.RULES[kudir.rule()], "записей": len(records),
            "итого": str(sum((r["income"] for r in records), 0)),
            "записи": [{**r, "income": str(r["income"])} for r in records[:200]]}


@router.post("/internal/ops/controls")
async def ops_controls_now(request: Request):
    """Регламенты руками (сообщения уходят сразу, отметки «сегодня уже» не ставятся).

        scripts/api.sh ops/controls            контроли + сводка в Ops
        scripts/api.sh 'ops/controls?what=morning'   утренний список менеджеру
        scripts/api.sh 'ops/controls?what=close'     закрытие прошлого месяца в Ops
        scripts/api.sh 'ops/controls?what=ads'       напоминание о рекламе за вчера
        scripts/api.sh 'ops/controls?what=taxes'     напоминания налогового календаря
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.ops import controls

    what = request.query_params.get("what") or "controls"
    jobs = {
        "controls": lambda: controls.check_controls(force_summary=True),
        "morning": lambda: controls.morning_list(force=True),
        "close": lambda: controls.month_close(force=True),
        "ads": lambda: controls.ads_reminder(force=True),
        "taxes": lambda: controls.tax_reminders(),
    }
    if what not in jobs:
        return {"error": f"what — одно из: {', '.join(jobs)}"}
    try:
        return await jobs[what]()
    except Exception as error:
        logger.exception("Регламент %s руками не выполнился", what)
        return {"error": _hide_token(f"{type(error).__name__}: {error}")[:300]}


@router.post("/internal/vk/token-permissions")
async def vk_token_permissions(request: Request):
    """Права токена сообщества (groups.getTokenPermissions) — только чтение.

        scripts/api.sh vk/token-permissions

    Нужно для отзывов (C7): есть право wall — бот может публиковать отзыв на стене
    после кнопки менеджера (REVIEW_PUBLISH_MODE=wall), нет — только вручную.
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.dialog import vk_client

    try:
        permissions = await vk_client.token_permissions()
    except Exception as error:
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    return {"права": permissions, "стена (wall)": "wall" in permissions,
            "режим публикации отзывов": settings.review_publish_mode}


@router.post("/internal/orders/{order_id}/ozon-approve")
async def ozon_approve_now(order_id: int, request: Request):
    """Автоподтверждение Ozon по заказу (C8) — как после сборки. В allowlist — dry-run."""
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.delivery import ozon_approve

    return {"заказ": order_id, **(await ozon_approve.after_packed(order_id))}


@router.post("/internal/referral/code")
async def referral_code(request: Request):
    """Код для друзей тестового аккаунта (проверка пакета D в бою).

        scripts/api.sh 'referral/code?peer_id=<тестовый VK ID>'
    """
    if not await _authorized(request):
        return Response(content="forbidden", media_type="text/plain", status_code=403)
    from app.modules.referral import service as referral

    try:
        peer_id = int(request.query_params.get("peer_id") or 0)
    except ValueError:
        return {"error": "peer_id — число"}
    if peer_id not in await analytics_service.test_peer_ids():
        return {"error": "только для тестовых аккаунтов (TEST_VK_IDS)"}
    row = await referral.code_for(peer_id)
    if row is None:
        return {"код": None}
    return {"код": row.code, "ссылка": referral.link(row.code), "id": row.id}
