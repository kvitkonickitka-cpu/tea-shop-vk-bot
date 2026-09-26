from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.modules.orders.models import Order, OrderPayment
from app.modules.orders.state import OrderDraft


def _details_of(draft: OrderDraft) -> dict:
    """Детали черновика для заказа — вместе с подписью доставки.

    Подпись доставки попадает в первый чек («Доставка: Ozon, пункт выдачи:
    …»), и закрывающий чек при вручении должен повторить ту же позицию.
    Своей колонки у заказа под неё нет, поэтому кладём в детали.
    """
    details = dict(draft.details or {})
    if draft.delivery_label:
        details["delivery_label"] = draft.delivery_label
    return details


async def save_order(
    peer_id: int,
    draft: OrderDraft,
    cdek_uuid: str | None = None,
    ozon_posting: str | None = None,
    status: str = "confirmed",
    payment_id: str | None = None,
    payment_status: str | None = None,
) -> Order:
    session_factory = get_session_factory()
    total = draft.items_total + (draft.delivery_cost or 0)

    async with session_factory() as session:
        order = Order(
            peer_id=peer_id,
            items=draft.items,
            items_total=draft.items_total,
            delivery_method=draft.delivery_method,
            delivery_cost=draft.delivery_cost,
            total=total,
            status=status,
            cdek_uuid=cdek_uuid,
            ozon_posting=ozon_posting,
            payment_id=payment_id,
            payment_status=payment_status,
            # Копия деталей черновика: отправление может заводиться позже,
            # когда черновик уже убран.
            details=_details_of(draft),
        )
        session.add(order)
        await session.commit()
    # Возвращаем сам заказ: у него есть номер, а карточку в чат заказов
    # отправляет вызывающий — сразу, не дожидаясь сверки по таймеру.
    return order


async def reopen_for_payment(order_id: int, draft: OrderDraft, payment_id: str, payment_status: str) -> Order | None:
    """Выставить новый счёт по тому же заказу.

    Прежний счёт истёк или не прошёл, клиент подтвердил заказ снова — но это
    тот же заказ: номер клиент уже видел в переписке, и менять его незачем.
    Обновляем состав и доставку (клиент мог что-то поправить), ставим новый
    платёж и стираем отметки напоминаний: по новому счёту они свои.
    """
    session_factory = get_session_factory()
    total = draft.items_total + (draft.delivery_cost or 0)
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return None
        order.items = draft.items
        order.items_total = draft.items_total
        order.delivery_method = draft.delivery_method
        order.delivery_cost = draft.delivery_cost
        order.total = total
        order.details = _details_of(draft)
        order.status = "awaiting_payment"
        order.payment_id = payment_id
        order.payment_status = payment_status
        order.reminder_1_sent_at = None
        order.reminder_2_sent_at = None
        await session.commit()
        await session.refresh(order)
        return order


async def by_id(order_id: int) -> Order | None:
    """Заказ по номеру — им пользуются служебные эндпоинты."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        return await session.get(Order, order_id)


async def register_payment(
    order_id: int, payment_id: str, *, attempt: int, status: str, amount: float
) -> None:
    """Записать попытку оплаты заказа.

    Заказ хранит только последний платёж, а уведомления приходят по всем: по
    истёкшему счёту клиент всё ещё может заплатить, потому что отменить
    pending у ЮKassa нельзя. Без этой записи такое уведомление не нашло бы
    заказ вовсе.
    """
    session_factory = get_session_factory()
    statement = (
        insert(OrderPayment)
        .values(
            payment_id=payment_id, order_id=order_id, attempt=attempt,
            status=status, amount=amount,
        )
        .on_conflict_do_update(
            index_elements=[OrderPayment.payment_id],
            set_={"status": status},
        )
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()


async def by_payment(payment_id: str) -> Order | None:
    """Заказ по идентификатору платежа — по нему приходит уведомление.

    Ищем через таблицу попыток, а не по `orders.payment_id`: там лежит
    только последний счёт, и оплата по предыдущему иначе осталась бы
    ничьей. Поиск по самому заказу оставлен как запасной — для строк,
    созданных до появления таблицы.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        order_id = await session.scalar(
            select(OrderPayment.order_id).where(OrderPayment.payment_id == payment_id)
        )
        if order_id is not None:
            return await session.get(Order, order_id)
        return (
            await session.execute(select(Order).where(Order.payment_id == payment_id))
        ).scalars().first()


async def payment_of(payment_id: str) -> OrderPayment | None:
    """Запись о конкретной попытке оплаты."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        return await session.get(OrderPayment, payment_id)


async def payments_of(order_id: int) -> list[OrderPayment]:
    """Все попытки оплаты заказа, от первой к последней."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(OrderPayment)
                .where(OrderPayment.order_id == order_id)
                .order_by(OrderPayment.attempt)
            )
        ).scalars().all()
    return list(rows)


async def next_attempt(order_id: int) -> int:
    """Номер следующей попытки оплаты этого заказа."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        last = await session.scalar(
            select(func.max(OrderPayment.attempt)).where(OrderPayment.order_id == order_id)
        )
    return int(last or 0) + 1


async def close_payment(payment_id: str, *, refund_id: str | None = None) -> None:
    """Отметить попытку закрытой с нашей стороны.

    У ЮKassa платёж при этом может оставаться оплачиваемым — отметка нужна
    нам, чтобы понимать, какой счёт мы клиенту уже аннулировали.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        row = await session.get(OrderPayment, payment_id)
        if row is None:
            return
        row.closed_at = datetime.now(timezone.utc)
        if refund_id:
            row.refund_id = refund_id
        await session.commit()


# Статус платежа у ЮKassa, означающий «деньги у нас».
PAID = "succeeded"
# Статус заказа, отменённого клиентом до оплаты (`orders/cancellation.py`).
CANCELED = "canceled"


async def claim_paid(
    payment_id: str, payment_status: str, receipt_status: str, order_id: int | None = None
) -> Order | None:
    """Пометить заказ оплаченным — ровно один раз.

    ЮKassa повторяет уведомление, пока не получит 200, а контейнер работает
    в несколько потоков: два уведомления могут обрабатываться одновременно.
    Проверка «если не оплачен, то пометить» двумя отдельными запросами это
    не спасает — между ними успевает влезть второй обработчик, и отправление
    заведётся дважды, то есть уедут две настоящие посылки.

    Поэтому переход делается одним `UPDATE ... WHERE`: кто получил строку в
    ответе, тот и заводит отправление. Остальным вернётся None, и это не
    ошибка.

    Условие смотрит на `payment_status`, а не на `status`. `status` после
    оплаты живёт своей жизнью — у заказа СДЭКом он уходит в «ждём ответа
    перевозчика», и заказ снова стал бы годен для повторного уведомления,
    то есть уехали бы две настоящие посылки. `payment_status` меняется
    ровно здесь и ровно один раз.

    Отменённый клиентом заказ оплаченным не становится: отмена и оплата
    могли встретиться, и кто первый — тот и прав. Деньги по отменённому
    заказу возвращает `webhook.handle_paid`.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        if order_id is None:
            order_id = await session.scalar(
                select(OrderPayment.order_id).where(OrderPayment.payment_id == payment_id)
            )

        # Заказ ищем по номеру, а платёж записываем как оплаченный. Условие
        # по `orders.payment_id` не годилось бы: клиент мог заплатить по
        # предыдущему счёту, а там лежит последний, — и такая оплата не
        # прошла бы вовсе.
        condition = (
            (Order.id == order_id) if order_id is not None else (Order.payment_id == payment_id)
        )
        row = (
            await session.execute(
                update(Order)
                .where(
                    condition,
                    Order.payment_status != PAID,
                    Order.status.is_distinct_from(CANCELED),
                )
                .values(
                    status="paid",
                    payment_id=payment_id,
                    payment_status=payment_status,
                    receipt_status=receipt_status,
                )
                .returning(Order)
            )
        ).scalars().first()
        await session.commit()
        return row


async def set_state(order_id: int, **fields) -> None:
    """Дописать заказу то, что стало известно позже: накладную, статус чека."""
    if not fields:
        return
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(update(Order).where(Order.id == order_id).values(**fields))
        await session.commit()
