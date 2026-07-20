from app.core.database import get_session_factory
from app.modules.orders.models import Order
from app.modules.orders.state import OrderDraft


async def save_order(peer_id: int, draft: OrderDraft) -> None:
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
            status="confirmed",
        )
        session.add(order)
        await session.commit()
