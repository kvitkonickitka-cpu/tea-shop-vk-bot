"""Журнал воронки: путь клиента от первого сообщения до денег и после.

Нужен, чтобы посчитать, сколько людей дошло от «беру» до ссылки и от
ссылки до денег, какие кнопки и касания работают; отсюда же берёт события
аналитика (`analytics.v_funnel_events`). Запись не должна мешать ответу
клиенту: ошибка базы здесь — только строка в логе.

У каждого события — источник (`source`): text, button, code, storefront,
reminder, manager, carrier, yookassa. По умолчанию он берётся из хода:
нажатие кнопки ставит button на всё, что случится внутри нажатия.

События:
- `dialog_start` — первое сообщение клиента или первое после суток тишины;
- `take_shown` — под ответом поставлены кнопки «Взять» (`data.items`);
- `button:<действие>` — нажатие кнопки; `button_stale` — нажатие старой;
  у кнопок под повторными касаниями в `data.touch` — какое касание;
- `draft_created` — черновик заведён; `data.origin` — text, take, repeat,
  storefront, returning, button;
- `upsell_offered`, `upsell_accepted` — допродажа предложена и взята;
- `delivery_quoted` — посчитана доставка (`method`, `cost`);
- `point_chosen` — пункт выдачи закреплён;
- `recipient_set` — записан получатель;
- `invoice_auto` — счёт выставлен кодом, как только заказ стал полным;
- `invoice_confirmed` — счёт после «да» на «Оформляем?» (confirm_order);
- `invoice_repeat` — счёт по «Повторить» одним нажатием;
- `invoice_returning` — счёт постоянному клиенту «как в прошлый раз»;
- `invoice_manual` — счёт командой менеджера; у всех счетов `attempt`;
- `payment_reminder_1`, `payment_reminder_2` — напоминания об оплате;
- `invoice_expired` — срок счёта вышел; `invoice_canceled` — отменил магазин;
- `payment_declined` — банк отказал (`reason`, `party` от ЮKassa);
- `payment_succeeded` — деньги пришли (`method`, `amount`, `income`);
- `shipment_created` — отправление заведено у перевозчика (`carrier`);
- `handed_over`, `at_pickup_point`, `delivered`, `not_delivered` — судьба посылки;
- `refunded` — возврат; `canceled_by_client` — клиент отменил заказ сам;
- `rated` (`value`), `review_saved` — оценка и отзыв;
- `escalation_opened` (`complaint`), `escalation_closed` — вопрос менеджеру;
- `opted_out` — «стоп» на продающие сообщения;
- `touch:<касание>` — повторное касание отправлено (`orders/retention.py`);
- `touch_order` — оплаченный заказ в течение 7 дней после касания;
- `touch_optout` — отписка в течение 2 дней после касания.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging

from app.core.database import get_session_factory
from app.messages.models import FunnelEvent

logger = logging.getLogger(__name__)


TEXT = "text"
BUTTON = "button"
CODE = "code"
STOREFRONT = "storefront"
REMINDER = "reminder"
MANAGER = "manager"
CARRIER = "carrier"
YOOKASSA = "yookassa"

# Источник по умолчанию для всего, что происходит в текущем ходе: нажатие
# кнопки ставит button, и черновик, пункт или счёт внутри нажатия получают
# его сами — без протаскивания параметра через все инструменты.
_source: contextvars.ContextVar[str] = contextvars.ContextVar("funnel_source", default=TEXT)


@contextlib.contextmanager
def source(value: str):
    token = _source.set(value)
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get()


async def record(
    peer_id: int, event: str, *, order_id: int | None = None, at=None, source_: str | None = None, **data
) -> None:
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
                source=source_ or _source.get(),
            )
            if at is not None:
                row.created_at = at
            session.add(row)
            await session.commit()
    except Exception:
        logger.exception("Не записали событие воронки %s для peer_id=%s", event, peer_id)
