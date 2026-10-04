"""«Как в прошлый раз», собранное кодом заранее: пункт, цена, получатель.

Постоянному клиенту раньше задавали три вопроса подряд — «туда же?», «на те
же данные?», «оформляем?». Теперь код до вопроса сам проверяет прошлый
пункт (доступен ли он, сколько стоит доставка сейчас), берёт прошлого
получателя и снова проверяет почту по DNS, применяет порог бесплатной
доставки — и клиент видит одно сообщение со всем заказом.

Предложение лежит в `details.offer` черновика и в сам заказ не пишется,
пока клиент не нажал «Оформить» или не ответил «да» (задача 5). Кнопка
«Повторить» под напоминанием — явное намерение, там счёт выставляется
сразу (задача 6).
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from app.modules.orders import contacts, eta, points
from app.modules.orders.repeat_delivery import LastDelivery, LastRecipient
from app.modules.orders.state import OrderDraft

logger = logging.getLogger(__name__)


@dataclass
class Offer:
    method: str = ""
    city: str = ""
    point_id: str | int | None = None
    point_address: str = ""
    carrier_cost: float | None = None
    tariff_code: int | None = None
    name: str = ""
    phone: str = ""
    email: str = ""
    quoted_at: float = 0.0
    # Срок перевозчика рядом с ценой (app/modules/orders/eta.py).
    eta: dict | None = None
    # Что не сошлось: пункт недоступен, почта не прошла проверку.
    problems: list[str] = field(default_factory=list)
    point_ok: bool = False
    recipient_ok: bool = False

    @property
    def ready(self) -> bool:
        return self.point_ok and self.recipient_ok

    @property
    def label(self) -> str:
        if self.method == "ozon_pvz":
            return f"Ozon, пункт выдачи: {self.point_address}"
        if self.method == "cdek_pvz":
            return f"СДЭК, пункт выдачи: {self.point_address}"
        return "СДЭК, курьером до адреса"

    def to_details(self) -> dict:
        return asdict(self)

    @classmethod
    def from_details(cls, raw: dict | None) -> "Offer | None":
        if not raw:
            return None
        known = {key: raw[key] for key in cls.__dataclass_fields__ if key in raw}
        return cls(**known)


def _eta(carrier: str, days_min, days_max, *, working: bool) -> dict | None:
    holder: dict = {}
    eta.remember(holder, carrier=carrier, days_min=days_min, days_max=days_max, working=working)
    return holder.get(eta.KEY)


async def prepare(
    draft: OrderDraft, delivery: LastDelivery | None, recipient: LastRecipient | None
) -> Offer:
    """Проверить прошлый пункт и получателя под новый состав."""
    from app.modules.orders import conversation

    offer = Offer()
    if delivery is not None:
        offer.method, offer.city = delivery.method, delivery.city
        offer.point_id, offer.point_address = delivery.point_id, delivery.place
        try:
            if delivery.method == "ozon_pvz" and delivery.point_id:
                quote = await conversation._ozon_price(draft, int(delivery.point_id))
                offer.carrier_cost = quote.total
                offer.eta = _eta("ozon", quote.days, quote.days, working=False)
            elif delivery.method == "cdek_pvz" and delivery.point_id:
                tariff, total = await conversation._cdek_delivery(
                    draft, "cdek_pvz", delivery.city, delivery_point=delivery.point_id
                )
                offer.carrier_cost, offer.tariff_code = total, tariff.code
                offer.eta = _eta("cdek", tariff.period_min, tariff.period_max, working=True)
            elif delivery.method == "cdek_courier":
                tariff, total = await conversation._cdek_delivery(draft, "cdek_courier", delivery.place)
                offer.carrier_cost, offer.tariff_code = total, tariff.code
                offer.eta = _eta("cdek", tariff.period_min, tariff.period_max, working=True)
            else:
                offer.problems.append("прошлый пункт выдачи неизвестен")
        except Exception as error:
            logger.info("Прошлый пункт %s недоступен: %s", delivery.point_id or delivery.method, type(error).__name__)
            if _is_timeout(error):
                # 04.10.2026: Ozon не ответил за отведённое время, а клиент
                # прочитал «пункт не принимает посылки» — и через минуту тот же
                # пункт посчитался. Таймаут — не отказ пункта.
                offer.problems.append(
                    f"доставку в прошлый пункт «{delivery.place}» сейчас не посчитали — перевозчик "
                    "не ответил вовремя; спроси, везти ли туда же (посчитаем ещё раз) или в другой пункт"
                )
            else:
                offer.problems.append(f"прошлый пункт «{delivery.place}» сейчас не принимает посылки")
        offer.point_ok = offer.carrier_cost is not None
        offer.quoted_at = time.time()
    else:
        offer.problems.append("прошлой доставки нет")

    if recipient is not None:
        offer.name = recipient.name
        offer.phone = contacts.normalize_phone(recipient.phone) or ""
        checked = await contacts.check_email(recipient.email) if recipient.email else None
        offer.email = checked.email if checked is not None and checked.ok else ""
        offer.recipient_ok = bool(offer.name and offer.phone and offer.email)
        if not offer.recipient_ok:
            offer.problems.append("данные прошлого получателя не прошли проверку — спроси новые")
    else:
        offer.problems.append("прошлого получателя нет")
    return offer


def _is_timeout(error: Exception) -> bool:
    import asyncio

    import httpx

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return True
    return "timeout" in str(error).casefold()


def client_delivery_cost(draft: OrderDraft, offer: Offer) -> float:
    """Сколько за доставку заплатит клиент — с порогом бесплатной доставки."""
    from app.modules.orders import conversation

    probe = OrderDraft(
        items=draft.items, items_total=draft.items_total,
        delivery_cost=offer.carrier_cost, details={},
    )
    conversation._apply_free_delivery(probe)
    return float(probe.delivery_cost or 0)


def apply(draft: OrderDraft, offer: Offer) -> None:
    """Записать предложение в черновик — только после «Оформить» или «да»."""
    from app.modules.orders import conversation

    details = draft.details
    for key in ("ozon_point_id", "ozon_point_address", "delivery_point", "carrier_delivery_cost"):
        details.pop(key, None)
    points.forget(details)
    draft.delivery_method = offer.method
    draft.delivery_label = offer.label
    draft.delivery_cost = offer.carrier_cost
    details["address"] = offer.city
    if offer.method == "ozon_pvz":
        details["ozon_point_id"] = int(offer.point_id)
        details["ozon_point_address"] = offer.point_address
    elif offer.method == "cdek_pvz":
        details["delivery_point"] = offer.point_id
    if offer.tariff_code:
        details["tariff_code"] = offer.tariff_code
    details["recipient_name"] = offer.name
    details["recipient_phone"] = offer.phone
    details["recipient_email"] = offer.email
    if offer.eta:
        details[eta.KEY] = dict(offer.eta)
    else:
        eta.forget(details)
    details.pop("offer", None)
    draft.stage = "awaiting_confirmation"
    conversation._apply_free_delivery(draft)
    details["quoted_at"] = offer.quoted_at or time.time()
    details["seen_total"] = draft.items_total + (draft.delivery_cost or 0)
