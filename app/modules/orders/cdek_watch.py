"""Догляд за заказами, отправленными в СДЭК.

`POST /v2/orders` отвечает `202 Accepted` — это «заявку поставили в очередь»,
а не «заказ создан». Проверку СДЭК делает асинхронно, уже после ответа, и
если она не прошла, заказа не будет: клиенту сказано «оформлен», в личном
кабинете пусто, и без этой проверки об этом не узнаёт никто.

Спрашивать состояние сразу после отправки бесполезно — заявка ещё в
обработке. Поэтому ходим по таймеру, тем же, что рассылает мини-отчёты.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.delivery import cdek_client
from app.modules.dialog import telegram_client, vk_client
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)

# Статусы в колонке `status`. Заводить отдельную колонку под результат
# проверки не стали: самодельных миграций и так три, а `status` ровно для
# этого и существует.
STATUS_NEW = "confirmed"
STATUS_REGISTERED = "cdek_registered"
STATUS_REJECTED = "cdek_rejected"
STATUS_STUCK = "cdek_stuck"
# Заказ не через СДЭК: проверять нечего, но в чат о нём сказать надо.
STATUS_REPORTED = "reported"

_DELIVERY_LABELS = {
    "cdek_pvz": "СДЭК, пункт выдачи",
    "cdek_courier": "СДЭК, курьером до адреса",
    "ozon_pvz": "Ozon ПВЗ",
    "russian_post": "Почта России",
}

# Сколько ждать, прежде чем считать зависшую заявку проблемой. СДЭК обычно
# управляется за секунды; полчаса — это уже не «ещё обрабатывается».
_STUCK_AFTER = timedelta(minutes=30)
# Заказы старше этого срока не трогаем: если за сутки никто не заметил,
# проверка уже не поможет, а дёргать СДЭК по кругу незачем.
_GIVE_UP_AFTER = timedelta(days=1)
# Потолок на один проход, чтобы не уткнуться в лимиты СДЭКа.
_BATCH = 20


def _errors_of(data: dict) -> list[str]:
    errors = []
    for request in data.get("requests") or []:
        for error in request.get("errors") or []:
            errors.append(cdek_client.describe_request_error(error))
    return errors


def _is_successful(data: dict) -> bool:
    entity = data.get("entity") or {}
    if entity.get("cdek_number"):
        return True
    return any(
        request.get("state") == "SUCCESSFUL"
        for request in data.get("requests") or []
        if request.get("type") == "CREATE"
    )


def _is_rejected(data: dict) -> bool:
    return any(
        request.get("state") == "INVALID"
        for request in data.get("requests") or []
    )


def _orders_chat() -> str | None:
    # None означает «чат менеджера по умолчанию»: пока отдельный чат не
    # заведён, сообщения о заказах всё равно должны доходить.
    return settings.telegram_orders_chat_id or None


async def _send(order: Order, text: str) -> None:
    try:
        await telegram_client.send_message(text, chat_id=_orders_chat())
    except Exception:
        logger.exception("Не смогли написать в чат заказов про заказ %s", order.id)


def _order_card(order: Order, cdek_number: str | None = None) -> str:
    lines = [f"🧾 <b>Заказ №{order.id}</b>"]
    for item in order.items or []:
        total = item.get("price", 0) * item.get("quantity", 1)
        lines.append(f"{item.get('name', 'товар')} × {item.get('quantity', 1)} — {total} руб.")

    delivery = _DELIVERY_LABELS.get(order.delivery_method or "", order.delivery_method or "—")
    lines.append(f"Товары {order.items_total} руб. + доставка {order.delivery_cost} руб. = <b>{order.total} руб.</b>")
    lines.append(f"Доставка: {delivery}")
    if cdek_number:
        lines.append(f"Накладная СДЭК: <code>{cdek_number}</code>")
    if order.ozon_posting:
        lines.append(f"Отправление Ozon: <code>{order.ozon_posting}</code>")
    lines.append(f"Диалог: {vk_client.dialog_link(order.peer_id)}")
    return "\n".join(lines)


async def _warn_manager(order: Order, what: str, details: str) -> None:
    text = (
        f"⚠️ Заказ №{order.id} {what}\n"
        f"Клиенту сказано, что заказ оформлен — его надо завести руками.\n"
        f"{details}\n"
        f"Диалог: {vk_client.dialog_link(order.peer_id)}"
    )
    await _send(order, text)


async def check_pending_orders() -> dict:
    """Сверяет с СДЭКом заказы, судьба которых ещё не известна."""
    result = {"checked": 0, "registered": 0, "rejected": 0, "stuck": 0, "failed": 0, "reported": 0}

    try:
        session_factory = get_session_factory()
    except RuntimeError:
        # База недоступна — бот в резервном режиме, проверять нечего.
        logger.warning("Проверка заказов СДЭК пропущена: база недоступна")
        return result

    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Order)
                .where(
                    Order.status == STATUS_NEW,
                    Order.created_at > now - _GIVE_UP_AFTER,
                )
                .order_by(Order.created_at)
                .limit(_BATCH)
            )
        ).scalars().all()

        for order in rows:
            # Заказ не через СДЭК проверять не у кого: у Ozon ответ на создание
            # синхронный, номер отправления уже в карточке. Показываем и
            # закрываем вопрос.
            if not order.cdek_uuid:
                order.status = STATUS_REPORTED
                result["reported"] += 1
                await _send(order, _order_card(order))
                continue

            result["checked"] += 1
            try:
                data = await cdek_client.order_state(order.cdek_uuid)
            except Exception as error:
                logger.exception("Не узнали состояние заказа %s в СДЭКе", order.id)
                result["failed"] += 1
                # Иначе безнадёжный заказ опрашивался бы каждые пять минут
                # целые сутки. Так бывает, например, когда заказ удалили в
                # кабинете: для API его больше нет, и ответ всегда будет 400.
                if now - order.created_at > _STUCK_AFTER:
                    order.status = STATUS_STUCK
                    result["stuck"] += 1
                    await _warn_manager(
                        order,
                        "не отвечает в СДЭКе",
                        f"Состояние узнать не получается: {str(error)[:200]}\n"
                        f"Возможно, заказ удалили в кабинете. uuid {order.cdek_uuid}",
                    )
                continue

            if _is_rejected(data):
                errors = _errors_of(data) or ["СДЭК не сказал, что именно не так"]
                order.status = STATUS_REJECTED
                result["rejected"] += 1
                logger.error("СДЭК отклонил заказ %s: %s", order.id, "; ".join(errors))
                await _warn_manager(order, "отклонён СДЭКом", "\n".join(errors))
                continue

            if _is_successful(data):
                order.status = STATUS_REGISTERED
                result["registered"] += 1
                number = (data.get("entity") or {}).get("cdek_number")
                logger.info("Заказ %s подтверждён СДЭКом, номер %s", order.id, number)
                await _send(order, _order_card(order, number))
                continue

            # Ни «создан», ни «отклонён» — значит всё ещё висит в обработке.
            age = now - order.created_at
            if age > _STUCK_AFTER:
                order.status = STATUS_STUCK
                result["stuck"] += 1
                logger.error("Заказ %s висит в СДЭКе без ответа %s", order.id, age)
                await _warn_manager(
                    order,
                    "завис в СДЭКе без ответа",
                    f"Заявка отправлена {age.total_seconds() // 60:.0f} мин назад, "
                    f"ответа нет. uuid {order.cdek_uuid}",
                )

        await session.commit()

    return result
