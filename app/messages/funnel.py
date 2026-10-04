"""Журнал воронки: что клиент нажал и какие счета выставил код.

Нужен, чтобы потом посчитать, сколько людей дошло от «беру» до ссылки и от
ссылки до денег, и какие кнопки работают. Запись не должна мешать ответу
клиенту: ошибка базы здесь — только строка в логе.

События:
- `invoice_auto` — счёт выставлен кодом, как только заказ стал полным;
- `invoice_confirmed` — счёт после «да» на «Оформляем?» (confirm_order);
- `invoice_repeat` — счёт по «Повторить» / «Оформить» одним нажатием;
- `button:<действие>` — нажатие кнопки; `button_stale` — нажатие старой;
  у кнопок под повторными касаниями в `data.touch` — какое касание;
- `touch:<касание>` — повторное касание отправлено (`orders/retention.py`);
- `touch_order` — оплаченный заказ в течение 7 дней после касания;
- `touch_optout` — отписка в течение 2 дней после касания.
"""

from __future__ import annotations

import logging

from app.core.database import get_session_factory
from app.messages.models import FunnelEvent

logger = logging.getLogger(__name__)


async def record(peer_id: int, event: str, *, order_id: int | None = None, at=None, **data) -> None:
    """Дописать событие. `at` — когда оно случилось (повторные касания и тесты со сдвигом времени)."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    try:
        async with session_factory() as session:
            row = FunnelEvent(
                peer_id=peer_id, event=event, order_id=order_id,
                data={k: v for k, v in data.items() if v is not None} or None,
            )
            if at is not None:
                row.created_at = at
            session.add(row)
            await session.commit()
    except Exception:
        logger.exception("Не записали событие воронки %s для peer_id=%s", event, peer_id)
