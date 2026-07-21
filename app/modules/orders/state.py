from __future__ import annotations

from dataclasses import dataclass, field

from app.core.database import get_session_factory
from app.modules.orders.models import OrderDraftRow


@dataclass
class OrderDraft:
    items: list[dict] = field(default_factory=list)
    items_total: float = 0.0
    delivery_method: str | None = None
    delivery_label: str | None = None
    delivery_cost: float | None = None
    # collecting -> awaiting_delivery -> awaiting_confirmation -> confirmed
    stage: str = "collecting"


# Резервное хранилище на случай, если DATABASE_URL не настроен (например,
# локальная разработка) — переживёт только до перезапуска процесса.
_fallback_drafts: dict[int, OrderDraft] = {}


def _row_to_draft(row: OrderDraftRow) -> OrderDraft:
    return OrderDraft(
        items=row.items,
        items_total=float(row.items_total),
        delivery_method=row.delivery_method,
        delivery_label=row.delivery_label,
        delivery_cost=float(row.delivery_cost) if row.delivery_cost is not None else None,
        stage=row.stage,
    )


async def get_draft(peer_id: int) -> OrderDraft | None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return _fallback_drafts.get(peer_id)

    async with session_factory() as session:
        row = await session.get(OrderDraftRow, peer_id)
        return _row_to_draft(row) if row is not None else None


async def set_draft(peer_id: int, draft: OrderDraft) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        _fallback_drafts[peer_id] = draft
        return

    async with session_factory() as session:
        row = await session.get(OrderDraftRow, peer_id)
        if row is None:
            row = OrderDraftRow(peer_id=peer_id)
            session.add(row)
        row.items = draft.items
        row.items_total = draft.items_total
        row.delivery_method = draft.delivery_method
        row.delivery_label = draft.delivery_label
        row.delivery_cost = draft.delivery_cost
        row.stage = draft.stage
        await session.commit()


async def clear_draft(peer_id: int) -> None:
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        _fallback_drafts.pop(peer_id, None)
        return

    async with session_factory() as session:
        row = await session.get(OrderDraftRow, peer_id)
        if row is not None:
            await session.delete(row)
            await session.commit()
