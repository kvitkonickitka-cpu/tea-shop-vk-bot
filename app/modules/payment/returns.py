"""Возврат заказа, проданного с кодами маркировки, — после закрывающего чека.

Товар продан полным расчётом, и в закрывающем чеке у каждой единицы свой
код. Возврат кнопкой в кабинете ЮKassa для такого заказа неверен: ЮKassa
соберёт чек возврата по данным платежа — предоплату без кодов, — и коды в
«Честном знаке» не вернутся в оборот. Поэтому полный возврат такого заказа
идёт командой менеджера `scripts/api.sh orders/<N>/refund-with-codes`:
`POST /refunds` с `receipt`, в котором те же строки и те же коды, что в
закрывающем чеке. После успеха коды получают статус `returned`.

Только полный возврат. Возврат части заказа и особенно одной пачки из
проданного сета пока не автоматизирован: как оформлять чек, когда набор
расформирован, ещё уточняем в «Честном знаке» — такой возврат делает
менеджер, а бот, увидев его в уведомлении ЮKassa, пишет в чат заказов,
что проверить (`webhook._on_refund`).

До закрывающего чека всё как раньше: товар не продан, в чеке предоплаты
кодов не было, и полный возврат кнопкой в кабинете верен.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.modules.marking import packing
from app.modules.marking.models import SET, SOLD
from app.modules.orders import repository as orders_repository
from app.modules.orders.models import Order
from app.modules.payment import settlement, yookassa_client

logger = logging.getLogger(__name__)

# Отметка в `orders.details`: возврат с кодами запрошен нами. Ставится до
# запроса — уведомление `refund.succeeded` может прийти раньше, чем мы
# запишем номер возврата из ответа.
_MARK = "codes_return"


class NotReady(RuntimeError):
    """Возврат с кодами сделать нельзя. Текст — для менеджера."""


def idempotence_key(order_id: int) -> str:
    # Полный возврат по заказу бывает один: повтор команды после сбоя сети
    # не должен вернуть деньги дважды.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-return-with-codes:{order_id}"))


def has_set(order: Order, codes: list | None = None) -> bool:
    """Есть ли в заказе сет или набор — по кодам, а без них по каталогу."""
    if codes:
        return any(code.kind == SET for code in codes)
    from app.modules.catalog import service as catalog_service

    items = catalog_service.load_items()
    return any(
        catalog_service.kind_for(str(item.get("name", "")), items) == SET
        for item in order.items or []
    )


def is_ours(order: Order, refund: yookassa_client.Refund, paid: float | None) -> bool:
    """Этот возврат сделан командой с кодами, а не кнопкой в кабинете."""
    mark = (order.details or {}).get(_MARK) or {}
    if not mark:
        return False
    if mark.get("refund_id"):
        return mark["refund_id"] == refund.id
    # Номер ещё не записан: уведомление обогнало ответ. Наш возврат — полный.
    return paid is not None and round(float(refund.amount), 2) == round(float(paid), 2)


async def paid_amount(order: Order) -> float:
    payment = await orders_repository.payment_of(order.payment_id) if order.payment_id else None
    if payment is not None and payment.amount is not None:
        return float(payment.amount)
    return float(order.total or 0)


async def _remember(order: Order, **values) -> None:
    details = dict(order.details or {})
    details[_MARK] = {**(details.get(_MARK) or {}), **values}
    await orders_repository.set_state(order.id, details=details)
    order.details = details


async def refund_with_codes(order: Order) -> dict:
    """Полный возврат с чеком возврата, в котором коды каждой единицы."""
    try:
        if order.payment_status != orders_repository.PAID or not order.payment_id:
            raise NotReady("заказ не оплачен через ЮKassa — возвращать нечего")
        if order.status == "refunded":
            raise NotReady("по заказу уже был возврат")
        if order.settlement_receipt_status != settlement.SUCCEEDED:
            raise NotReady(
                "закрывающего чека с кодами нет — товар не продан, и возврат делается "
                "как раньше, кнопкой в кабинете ЮKassa (без кодов: их не было и в чеке предоплаты)"
            )
        codes = await packing.codes_of(order.id, statuses=(SOLD,))
        if not codes:
            raise NotReady("у заказа нет проданных кодов — проверьте закрывающий чек")
        receipt = settlement.return_receipt(order, codes)
    except (NotReady, settlement.NotReady) as reason:
        return {"возврат": "не сделан", "почему": str(reason)}

    paid = await paid_amount(order)
    total = yookassa_client.receipt_total(receipt["items"])
    if round(total, 2) != round(paid, 2):
        return {
            "возврат": "не сделан",
            "почему": f"сумма чека возврата {total:.2f} не сходится с оплатой {paid:.2f}",
        }

    await _remember(order, requested_at=datetime.now(timezone.utc).isoformat())
    try:
        refund = await yookassa_client.create_refund_with_receipt(
            payment_id=order.payment_id, amount=paid, receipt=receipt,
            key=idempotence_key(order.id),
        )
    except yookassa_client.YooKassaError as error:
        logger.warning("Заказ %s: возврат с кодами не создан — %s", order.id, error)
        return {"возврат": "не сделан", "почему": str(error)[:300],
                "что дальше": "повтор команды — с тем же ключом, деньги дважды не вернутся"}

    await _remember(order, refund_id=refund.id)
    returned = 0
    if refund.status == "succeeded":
        returned = await packing.mark_returned(order.id, refund.id)
    # Карточку менеджеру и сообщение клиенту пишет обработчик уведомления
    # `refund.succeeded` — так же, как при возврате из кабинета.
    return {"возврат": refund.id, "статус": refund.status, "сумма": refund.amount,
            "кодов в чеке возврата": len(codes), "отмечено возвращёнными": returned}
