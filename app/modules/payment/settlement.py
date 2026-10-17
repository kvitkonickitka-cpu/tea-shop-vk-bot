"""Закрывающий чек — зачёт предоплаты при вручении посылки.

Первый чек уходит при оплате: предоплата 100 %, без кодов маркировки. При
продаже с доставкой договор исполнен в момент вручения (ст. 499 ГК РФ), и
тогда нужен второй чек: те же позиции, «полный расчёт», у каждой пачки —
её код маркировки, а в `settlements` — зачёт предоплаты на всю сумму.

Что подтвердила поддержка ЮKassa 25.09.2026: второй чек — только отдельным
запросом `POST /v3/receipts`; `type=payment`, `payment_id`, `send=true`;
у всех позиций `full_payment`; `settlements` с `prepayment` на всю сумму;
в чеке предоплаты коды не передаём, во втором — код каждой пачки в
`mark_code_info`; почта обязательна; срока между оплатой и вторым чеком нет.

Три правила этого модуля:

- **один чек на заказ.** Пока есть чек в `pending` или `succeeded`, второй
  не создаётся. Ключ идемпотентности выводится из номера заказа и номера
  попытки: сбой сети повторяется с тем же ключом, а новая попытка после
  отказа ЮKassa — с новым;
- **без кодов чек не уходит.** Если заказ не собирали через страницу, чек
  не отправляется, а менеджеру — срочное уведомление. Исправить и
  отправить: страница сборки (`orders/<N>/pack-link`), потом
  `orders/<N>/settlement-receipt`;
- **тихие часы на чек не действуют** — они про сообщения клиенту. Чек
  уходит сразу по событию «вручено».

Каждая маркированная единица — своя строка с количеством 1 и своим кодом:
две одинаковые пачки — две строки, сет или набор — одна строка с кодом
идентификации набора (КИН) с коробки. Сумма чека совпадает с чеком
предоплаты до копейки: если цену позиции приходится делить на строки,
остаток копеек уходит на последнюю.

Здесь же — чек возврата для полного возврата после закрывающего чека
(`return_receipt`): те же строки с теми же кодами.

Всё — за настройкой `SETTLEMENT_RECEIPT_ENABLED`, по умолчанию выключено.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import templates
from app.modules.marking import packing
from app.modules.marking.models import OrderMarkingCode
from app.modules.orders import order_chat, repository as orders_repository
from app.modules.orders.models import Order
from app.modules.ops.journal import order_scope
from app.modules.payment import yookassa_client

logger = logging.getLogger(__name__)

# Статусы чека у нас. Три первых — ЮKassa, два последних — наши: «unknown»
# — ответа не было, повторить тем же ключом; «rejected» — ЮKassa отказала,
# повтор только с новой попытки.
PENDING = "pending"
SUCCEEDED = "succeeded"
CANCELED = "canceled"
UNKNOWN = "unknown"
REJECTED = "rejected"

_LIVE = (PENDING, SUCCEEDED)

# У «Чеков от ЮKassa» в чеке не больше 80 позиций. Каждая пачка — своя
# позиция, плюс доставка.
_MAX_ITEMS = 80
# Досылаем чек по вручённым заказам не дольше двух недель: дальше это
# разбор для менеджера, а не для таймера.
_RETRY_WITHIN = timedelta(days=14)

_PAYMENT_MODE = "full_payment"
_SUBJECT_MARKED = "marked"
# Режим обработки кода маркировки (тег 2102): для «Чеков от ЮKassa» —
# только «0», строкой (OpenAPI, схема MarkMode).
_MARK_MODE = "0"


class NotReady(RuntimeError):
    """Чек отправлять нельзя. Текст — для менеджера."""


def is_enabled() -> bool:
    return settings.settlement_receipt_enabled and yookassa_client.is_configured()


def was_sent(order: Order) -> bool:
    """Ушёл ли закрывающий чек — для текста клиенту о вручении."""
    return bool(order.settlement_receipt_id) and order.settlement_receipt_status in _LIVE


def encode_mark(code: str) -> str:
    """Код маркировки для `mark_code_info.gs_1m` — строкой как есть.

    Ответ поддержки ЮKassa 26.09.2026: только поле `gs_1m`, **без base64**,
    код целиком — с криптохвостом и разделителями GS. В JSON разделитель
    уходит как `\u001d`: так его записывает любой сериализатор JSON, потому
    что управляющие символы в строке экранируются всегда. Пример в их статье
    про чек зачёта выглядел как base64 и сбивал с толку — не он.
    """
    return code


def idempotence_key(order_id: int, attempt: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-settlement:{order_id}#{attempt}"))


def _money(value: float) -> dict:
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


def split_kopecks(total: int, parts: int) -> list[int]:
    """Разделить сумму в копейках на `parts` строк так, чтобы итог сошёлся.

    Поровну, а остаток копеек — на последнюю строку: 1000,00 ₽ на три
    строки — 333,33 + 333,33 + 333,34.
    """
    if parts <= 0:
        raise ValueError("строк должно быть больше нуля")
    base = total // parts
    return [base] * (parts - 1) + [total - base * (parts - 1)]


def _kopecks(money: dict) -> int:
    return round(float(money["value"]) * 100)


def _prepayment_rows(order: Order) -> tuple[list[dict], list[dict]]:
    """Позиции чека предоплаты — товары и доставка — той же функцией, что при оплате."""
    details = order.details or {}
    first = yookassa_client.receipt_items(
        list(order.items or []),
        float(order.delivery_cost or 0),
        details.get("delivery_label") or order_chat.DELIVERY_LABELS.get(
            order.delivery_method or "", order.delivery_method or ""
        ),
    )
    count = len(order.items or [])
    return first[:count], first[count:]


def marked_items(order: Order, codes: list[OrderMarkingCode]) -> list[dict]:
    """Строки чека с кодами маркировки: одна единица — одна строка.

    Позиции берутся из чека предоплаты (те же названия, цены и ставка НДС)
    и переделываются: строка позиции делится на столько строк, сколько у неё
    единиц, у каждой количество 1 и свой код, расчёт полный. Сет — одна
    единица со своим КИН: пачки внутри в чек не идут. Доставка — своей
    строкой, как в чеке предоплаты, но тоже полным расчётом.

    TODO(yookassa): формат второго чека и чека возврата с кодами — вопрос в
    поддержку ЮKassa ещё открыт. Сверено с OpenAPI ЮKassa 16.10.2026:
    `mark_code_info.gs_1m` — код целиком (до 200 символов), `mark_mode` —
    строка «0», `payment_subject: marked` — «товар с кодом маркировки».
    Не передаём, пока не ответят: `mark_quantity` (OpenAPI называет его
    обязательным при `measure: piece`, хотя это тег дробного количества —
    для целой пачки он не нужен) и `planned_status` (тег 2003, у ЮKassa
    «указывается только для маркированных», без слова «обязательно»).
    """
    goods, delivery = _prepayment_rows(order)

    by_position: dict[int, list[OrderMarkingCode]] = {}
    for row in codes:
        by_position.setdefault(row.item_index, []).append(row)

    items = []
    for index, row in enumerate(goods):
        position_codes = by_position.get(index, [])
        units = int(row["quantity"])
        if len(position_codes) != units:
            raise NotReady(
                f"по позиции «{row['description']}» кодов {len(position_codes)}, "
                f"а единиц {units}"
            )
        amounts = split_kopecks(_kopecks(row["amount"]) * units, units)
        for code, kopecks in zip(position_codes, amounts):
            items.append({
                "description": row["description"],
                "quantity": 1,
                "amount": _money(kopecks / 100),
                "vat_code": row["vat_code"],
                "payment_mode": _PAYMENT_MODE,
                "payment_subject": _SUBJECT_MARKED,
                "mark_mode": _MARK_MODE,
                "mark_code_info": {"gs_1m": encode_mark(code.raw_code)},
                "measure": "piece",
            })
    for row in delivery:
        # Доставка — с тем же признаком предмета расчёта, что в первом чеке
        # (услуга), только расчёт теперь полный.
        items.append({**row, "payment_mode": _PAYMENT_MODE})

    if len(items) > _MAX_ITEMS:
        raise NotReady(
            f"в чеке вышло {len(items)} позиций, а «Чеки от ЮKassa» принимают "
            f"не больше {_MAX_ITEMS} — чек придётся разбить вручную"
        )
    prepaid = yookassa_client.receipt_total(goods + delivery)
    total = yookassa_client.receipt_total(items)
    if total != prepaid:
        raise NotReady(
            f"сумма строк с кодами {total:.2f} не сходится с чеком предоплаты {prepaid:.2f}"
        )
    return items


def _customer(order: Order) -> dict:
    details = order.details or {}
    return yookassa_client.receipt_customer(
        full_name=details.get("recipient_name", ""),
        email=details.get("recipient_email", ""),
    )


def build_payload(order: Order, codes: list[OrderMarkingCode]) -> dict:
    """Тело `POST /receipts` для закрывающего чека.

    TODO(yookassa): второй чек (зачёт предоплаты с кодами) — формат
    подтверждён поддержкой 25–26.09.2026, но вопрос про второй чек и коды
    ещё открыт; проверить на тестовой отправке (docs/TEST_PLAN_marking.md).
    """
    items = marked_items(order, codes)
    total = yookassa_client.receipt_total(items)
    return {
        "type": "payment",
        "payment_id": order.payment_id,
        "customer": _customer(order),
        "send": True,
        "items": items,
        "settlements": [{"type": "prepayment", "amount": _money(total)}],
        "internet": True,
        "timezone": settings.yookassa_receipt_timezone,
    }


def return_receipt(order: Order, codes: list[OrderMarkingCode]) -> dict:
    """`receipt` для `POST /refunds` — полный возврат после закрывающего чека.

    Товар продан полным расчётом с кодами, поэтому и чек возврата прихода —
    те же строки, полный расчёт и код каждой единицы (по ФФД 1.2 коды в чеке
    возврата маркированного товара обязательны). Без `receipt` ЮKassa
    собрала бы чек по данным платежа — предоплату без кодов, — а это неверно.
    Доставка возвращается вместе с заказом: полный возврат — вся сумма.

    TODO(yookassa): поддержка 26.09.2026 ответила «при возврате передаётся
    `receipt` с возвращаемыми товарами и кодами» — то есть в `POST /refunds`,
    без отдельного чека. Сверить на тестовой отправке; `planned_status: 3`
    («штучный товар возвращён») пока не передаём — см. `marked_items`.
    """
    return {
        "customer": _customer(order),
        "items": marked_items(order, codes),
        "internet": True,
        "timezone": settings.yookassa_receipt_timezone,
    }


async def _check_ready(order: Order) -> list[OrderMarkingCode]:
    """Всё ли есть для чека. Коды заказа — если всё; иначе NotReady."""
    if order.payment_status != orders_repository.PAID or not order.payment_id:
        raise NotReady("заказ не оплачен через ЮKassa — закрывающий чек не нужен")
    if order.status == "refunded":
        raise NotReady(
            "по заказу был возврат — состав закрывающего чека не известен, "
            "его надо оформить вручную в кабинете ЮKassa"
        )
    if order.not_delivered_at is not None:
        raise NotReady("заказ не вручён — закрывающий чек не формируется")
    if not (order.details or {}).get("recipient_email"):
        raise NotReady("нет почты клиента — «Чеки от ЮKassa» без неё чек не примут")
    codes = await packing.codes_of(order.id)
    if order.packed_at is None or not codes:
        raise NotReady("нет кодов маркировки — заказ не собирали через страницу сборки")
    return codes


async def _save(order: Order, **fields) -> None:
    await orders_repository.set_state(order.id, **fields)
    for name, value in fields.items():
        setattr(order, name, value)


async def _tell_manager(order: Order, note: str, *, urgent: bool = True) -> None:
    """Сказать менеджеру — только когда причина новая, а не каждый тик."""
    if order.settlement_note == note:
        return
    await _save(order, settlement_note=note)
    await order_chat.send(order, templates.manager_settlement_problem(order, note, urgent=urgent))


@order_scope
async def issue(order: Order) -> dict:
    """Отправить закрывающий чек по заказу. Возвращает, что вышло."""
    if not is_enabled():
        return {"чек": "не отправлен", "почему": "SETTLEMENT_RECEIPT_ENABLED выключен или нет ключей ЮKassa"}

    status = order.settlement_receipt_status
    if order.settlement_receipt_id and status in _LIVE:
        return {"чек": order.settlement_receipt_id, "статус": status, "действий": "нет — чек уже есть"}

    try:
        codes = await _check_ready(order)
        payment = await orders_repository.payment_of(order.payment_id)
        payload = build_payload(order, codes)
        total = float(payload["settlements"][0]["amount"]["value"])
        paid = float(payment.amount) if payment is not None and payment.amount is not None else float(order.total or 0)
        if round(total, 2) != round(paid, 2):
            raise NotReady(
                f"сумма чека {total:.2f} не сходится с оплатой {paid:.2f} — "
                "состав заказа менялся после оплаты"
            )
    except NotReady as reason:
        logger.warning("Заказ %s: закрывающий чек не отправлен — %s", order.id, reason)
        await _tell_manager(order, f"вручено, закрывающий чек не сформирован — {reason}")
        return {"чек": "не отправлен", "почему": str(reason)}

    # Сбой сети — повтор тем же ключом; отказ или отмена — новая попытка.
    attempt = order.settlement_receipt_attempt or 0
    if status != UNKNOWN or attempt == 0:
        attempt += 1
    await _save(order, settlement_receipt_attempt=attempt)

    try:
        receipt = await yookassa_client.create_receipt(payload, idempotence_key(order.id, attempt))
    except yookassa_client.YooKassaUnknown as error:
        await _save(order, settlement_receipt_status=UNKNOWN)
        logger.warning("Заказ %s: ЮKassa не ответила про закрывающий чек — %s", order.id, error)
        return {"чек": "неизвестно", "почему": str(error)[:300], "что дальше": "повторит таймер тем же ключом"}
    except yookassa_client.YooKassaError as error:
        await _save(order, settlement_receipt_status=REJECTED)
        await _tell_manager(order, f"ЮKassa отказала в закрывающем чеке — {str(error)[:300]}")
        return {"чек": "отклонён", "почему": str(error)[:300]}

    await _save(
        order,
        settlement_receipt_id=receipt.id,
        settlement_receipt_status=receipt.status or PENDING,
        settlement_receipt_sent_at=datetime.now(timezone.utc),
        settlement_note=None,
    )
    if receipt.status == SUCCEEDED:
        await packing.mark_sold(order.id)
    logger.info("Заказ %s: закрывающий чек %s, статус %s", order.id, receipt.id, receipt.status)
    return {"чек": receipt.id, "статус": receipt.status, "попытка": attempt}


@order_scope
async def on_delivered(order: Order) -> dict:
    """Событие «вручено»: чек уходит сразу, тихие часы тут ни при чём."""
    if not is_enabled():
        return {"чек": "не отправлен", "почему": "выключено"}
    if order.payment_status != orders_repository.PAID:
        # Неоплаченный через ЮKassa заказ ведёт менеджер, чек ему не наш.
        return {"чек": "не нужен", "почему": "заказ не оплачен через ЮKassa"}
    return await issue(order)


async def check(now: datetime | None = None) -> dict:
    """Догляд за закрывающими чеками — тиком расписания.

    - `pending` — спросить ЮKassa; дольше суток — сказать менеджеру;
    - `canceled` — сказать менеджеру;
    - ответа не было (`unknown`) — повторить тем же ключом;
    - вручено, а чека нет и причины не записано (например, упал сам
      обработчик события) — попробовать отправить.
    """
    result = {"checked": 0, "succeeded": 0, "canceled": 0, "stuck": 0, "sent": 0}
    if not is_enabled():
        return result
    now = now or datetime.now(timezone.utc)
    stuck_after = timedelta(hours=settings.settlement_receipt_pending_alert_hours)

    session_factory = get_session_factory()
    async with session_factory() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.delivered_at.is_not(None),
                    Order.delivered_at > now - _RETRY_WITHIN,
                    Order.payment_status == orders_repository.PAID,
                    (Order.settlement_receipt_status.is_(None))
                    | Order.settlement_receipt_status.in_((PENDING, UNKNOWN)),
                ).limit(20)
            )
        ).scalars().all()

    for order in orders:
        result["checked"] += 1
        if order.settlement_receipt_status == PENDING and order.settlement_receipt_id:
            try:
                receipt = await yookassa_client.get_receipt(order.settlement_receipt_id)
            except Exception as error:
                logger.warning("Заказ %s: статус закрывающего чека не узнали — %s", order.id, error)
                continue
            if receipt.status == SUCCEEDED:
                await _save(order, settlement_receipt_status=SUCCEEDED)
                await packing.mark_sold(order.id)
                result["succeeded"] += 1
            elif receipt.status == CANCELED:
                await _save(order, settlement_receipt_status=CANCELED)
                result["canceled"] += 1
                await _tell_manager(order, "касса отменила закрывающий чек (canceled)")
            elif order.settlement_receipt_sent_at and now - order.settlement_receipt_sent_at > stuck_after:
                result["stuck"] += 1
                await _tell_manager(
                    order,
                    f"закрывающий чек висит в pending больше "
                    f"{settings.settlement_receipt_pending_alert_hours} ч",
                    urgent=False,
                )
            continue

        if order.settlement_receipt_status == UNKNOWN or not order.settlement_note:
            outcome = await issue(order)
            if outcome.get("статус") in _LIVE:
                result["sent"] += 1
    return result
