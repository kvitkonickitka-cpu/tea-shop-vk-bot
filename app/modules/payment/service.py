"""Оплата заказа: выставление счёта и статусы.

Пока `PAYMENTS_ENABLED=false`, модуль в работе не участвует — заказ на шаге
оплаты уходит менеджеру эскалацией. Когда флаг поднят, счёт выставляет
ЮKassa, и сюда же приходит ответ на вопрос «оплачено ли».
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.modules.orders.state import OrderDraft
from app.modules.payment import yookassa_client

logger = logging.getLogger(__name__)

# Статусы заказа вокруг оплаты. Живут в той же колонке `status`, что и
# статусы сверки с перевозчиками: заводить вторую колонку ради трёх значений
# незачем, а порядок состояний у заказа всё равно один.
STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_PAID = "paid"
# Оплату ждём не вечно: ссылка живёт ограниченное время, и висящий заказ
# лучше показать менеджеру, чем держать в ожидании бесконечно.
UNPAID_AFTER_HOURS = 24


def is_enabled() -> bool:
    return settings.payments_enabled and yookassa_client.is_configured()


async def create_payment(draft: OrderDraft, order_key: str) -> yookassa_client.Payment:
    """Выставить счёт по черновику. Исключения разбирает вызывающий."""
    details = draft.details
    return await yookassa_client.create_payment(
        order_key=order_key,
        items=draft.items,
        delivery_cost=draft.delivery_cost or 0,
        delivery_label=draft.delivery_label or "",
        email=details.get("recipient_email", ""),
        phone=details.get("recipient_phone", ""),
        full_name=details.get("recipient_name", ""),
        description=f"Заказ {order_key}",
    )


async def generate_payment_link(draft: OrderDraft) -> str:
    """Оставлено ради обратной совместимости со старым вызовом.

    Настоящее выставление счёта идёт через `create_payment`: ему нужен номер
    заказа для ключа идемпотентности, а по одному черновику его не собрать.
    """
    return (
        "Ссылка на оплату скоро будет готова — модуль оплаты ещё не "
        "подключён, менеджер свяжется с вами для оформления."
    )
