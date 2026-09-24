from sqlalchemy import select, update

from app.core.database import get_session_factory
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft


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
            details=dict(draft.details or {}),
        )
        session.add(order)
        await session.commit()
    # Возвращаем сам заказ: у него есть номер, а карточку в чат заказов
    # отправляет вызывающий — сразу, не дожидаясь сверки по таймеру.
    return order


async def by_payment(payment_id: str) -> Order | None:
    """Заказ по идентификатору платежа — по нему приходит уведомление."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        return (
            await session.execute(select(Order).where(Order.payment_id == payment_id))
        ).scalars().first()


# Статус платежа у ЮKassa, означающий «деньги у нас».
PAID = "succeeded"


async def claim_paid(payment_id: str, payment_status: str, receipt_status: str) -> Order | None:
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
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(
                update(Order)
                .where(Order.payment_id == payment_id, Order.payment_status != PAID)
                .values(status="paid", payment_status=payment_status, receipt_status=receipt_status)
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
