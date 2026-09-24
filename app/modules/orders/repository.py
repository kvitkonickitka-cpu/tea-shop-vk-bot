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
        )
        session.add(order)
        await session.commit()
    # Возвращаем сам заказ: у него есть номер, а карточку в чат заказов
    # отправляет вызывающий — сразу, не дожидаясь сверки по таймеру.
    return order
