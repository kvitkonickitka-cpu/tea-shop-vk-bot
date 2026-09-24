"""Короткие сообщения клиенту о судьбе уже оформленного заказа.

Отдельным модулем, потому что отправителей несколько и все они работают
вне диалога: уведомление ЮKassa, догляд за платежами, сверка заказов СДЭКа.
Клиента в переписке в этот момент нет, формулировать нечего — весь текст
известен заранее, и звать ради него Claude незачем.

Раньше такие новости уходили только менеджеру в телеграм: клиент, у
которого отменился счёт, завис чек или не поехала посылка, не узнавал
ничего. Для него это выглядело как «заплатил и пропал».
"""

from __future__ import annotations

import logging

from app.modules.dialog import vk_client

logger = logging.getLogger(__name__)


def amount(value) -> str:
    """Сумма без хвоста «.0»: 917 руб., а не 917.0 руб."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


async def tell(order, text: str) -> bool:
    """Написать клиенту. False — если не дошло: это не повод падать выше.

    Вызывающие работают по уведомлению или по таймеру, и ошибка отправки не
    должна ни отменять уже сделанное, ни заставлять ЮKassa слать уведомление
    сутки подряд.
    """
    try:
        await vk_client.send_message(order.peer_id, text)
        return True
    except Exception:
        logger.exception("Не написали клиенту про заказ %s", order.id)
        return False


def payment_canceled(order) -> str:
    return (
        f"Счёт по заказу №{order.id} на {amount(order.total)} руб. отменён — "
        "оплата не прошла.\nЕсли заказ ещё нужен, напишите сюда: выставим "
        "счёт заново 🙏"
    )


def payment_expired(order) -> str:
    return (
        f"Ссылка на оплату заказа №{order.id} больше не действует — прошли "
        "сутки.\nЕсли чай ещё нужен, напишите сюда, и мы оформим заказ "
        "заново по актуальным ценам."
    )


def refunded(order, refund_amount) -> str:
    return (
        f"Возврат по заказу №{order.id} оформлен: {amount(refund_amount)} руб.\n"
        "Деньги вернутся тем же способом, которым вы платили — срок зачисления "
        "зависит от банка."
    )


def receipt_delayed(order) -> str:
    email = (order.details or {}).get("recipient_email")
    where = f" на {email}" if email else ""
    return (
        f"Чек по заказу №{order.id} пока не пришёл — задержка на стороне "
        f"кассы. Мы уже разбираемся, чек придёт{where}."
    )


def shipment_trouble(order) -> str:
    """Посылка не поехала у перевозчика, а деньги клиент уже отдал."""
    return (
        f"По заказу №{order.id} задержка с оформлением доставки — менеджер уже "
        "разбирается и напишет вам.\nОплата у нас, заказ никуда не потерялся 🙏"
    )
