from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrderDraft:
    items: list[dict] = field(default_factory=list)
    items_total: float = 0.0
    delivery_method: str | None = None
    delivery_label: str | None = None
    delivery_cost: float | None = None
    # collecting -> awaiting_delivery -> awaiting_confirmation -> confirmed
    stage: str = "collecting"


# В памяти процесса: переживёт до перезапуска/передеплоя, не шарится между
# инстансами. Для MVP достаточно, при росте — переезд на Redis/БД.
_drafts: dict[int, OrderDraft] = {}


def get_draft(peer_id: int) -> OrderDraft | None:
    return _drafts.get(peer_id)


def set_draft(peer_id: int, draft: OrderDraft) -> None:
    _drafts[peer_id] = draft


def clear_draft(peer_id: int) -> None:
    _drafts.pop(peer_id, None)
