"""ЮKassa: платёж вместе с чеком и его состояние.

Три вещи, которые определяют устройство этого модуля:

- **`Idempotence-Key` обязателен для POST.** Тот же ключ и те же данные —
  повтор, другой ключ — новый платёж. Поэтому ключ здесь не случайный, а
  выводится из номера заказа: повторная попытка выставить оплату по тому же
  заказу вернёт тот же платёж, а не спишет с клиента дважды;
- **HTTP 500 не означает неудачу.** Если ЮKassa не может ответить точно за
  30 секунд, она отвечает 500 и пытается отменить операцию. Что случилось на
  самом деле, показывает только запрос состояния, поэтому 500 поднимается
  отдельным исключением: молча считать платёж непрошедшим нельзя;
- **чек едет в том же запросе**, в объекте `receipt`. ЮKassa лишь проверяет
  данные, а регистрирует чек касса с ОФД — позже и асинхронно.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Столько ЮKassa отводит себе на точный ответ; ждём чуть дольше, чтобы
# получить её 500, а не оборвать соединение самим и гадать.
_TIMEOUT_SECONDS = 35

# Ограничения из спецификации: название позиции 1–128 символов.
_MAX_ITEM_NAME = 128
# Ставка НДС и признаки расчёта. Предоплата: клиент платит до отправки.
_PAYMENT_MODE = "full_prepayment"
_SUBJECT_GOODS = "commodity"
_SUBJECT_SERVICE = "service"


class YooKassaError(RuntimeError):
    """Отказ ЮKassa: неверные данные, отклонённый платёж, недоступность."""


class YooKassaUnknown(YooKassaError):
    """Ответа нет, и что стало с платежом — неизвестно.

    Отдельный тип, чтобы вызывающий не принял это за «платёж не прошёл»:
    деньги могли быть списаны. Такой заказ нужно проверять запросом
    состояния, а не выставлять оплату заново.
    """


@dataclass(frozen=True)
class Payment:
    id: str
    status: str
    paid: bool
    confirmation_url: str
    receipt_registration: str
    test: bool
    amount: float


def is_configured() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


def normalize_phone(raw: str) -> str:
    """Телефон цифрами, как требует ITU-T E.164: `79001234567`.

    Клиент пишет телефон как удобно — со скобками, плюсом, пробелами, через
    восьмёрку. ЮKassa принимает только цифры, и такой чек уйдёт в отказ.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


def idempotence_key(order_key: str) -> str:
    """Ключ, выведенный из заказа, а не случайный.

    Случайный ключ на повторной попытке создал бы второй платёж — то есть
    списал бы с клиента дважды. Один и тот же заказ обязан давать один ключ.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-order:{order_key}"))


def _money(value: float) -> dict:
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


def receipt_items(items: list[dict], delivery_cost: float, delivery_label: str) -> list[dict]:
    """Позиции чека: товары плюс доставка отдельной строкой.

    Доставка — услуга, а не товар, и в чеке она обязана быть своей позицией:
    сумма платежа должна сходиться с суммой позиций чека до копейки.
    """
    rows = []
    for item in items:
        quantity = item.get("quantity", 1)
        price = float(item.get("price", 0))
        rows.append(
            {
                "description": str(item.get("name", "Товар"))[:_MAX_ITEM_NAME],
                "quantity": quantity,
                "amount": _money(price * quantity),
                "vat_code": settings.yookassa_vat_code,
                "payment_mode": _PAYMENT_MODE,
                "payment_subject": _SUBJECT_GOODS,
                "measure": "piece",
            }
        )

    if delivery_cost:
        rows.append(
            {
                "description": f"Доставка: {delivery_label or 'услуга доставки'}"[:_MAX_ITEM_NAME],
                "quantity": 1,
                "amount": _money(delivery_cost),
                "vat_code": settings.yookassa_vat_code,
                "payment_mode": _PAYMENT_MODE,
                "payment_subject": _SUBJECT_SERVICE,
                "measure": "piece",
            }
        )
    return rows


def _describe_failure(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"

    parts = [f"HTTP {response.status_code}"]
    if data.get("code"):
        parts.append(str(data["code"]))
    if data.get("description"):
        parts.append(str(data["description"]))
    if data.get("parameter"):
        parts.append(f"поле {data['parameter']}")
    return " — ".join(parts) if len(parts) > 1 else f"{parts[0]}: {str(data)[:300]}"


def _to_payment(data: dict) -> Payment:
    return Payment(
        id=data.get("id", ""),
        status=data.get("status", ""),
        paid=bool(data.get("paid")),
        confirmation_url=(data.get("confirmation") or {}).get("confirmation_url", ""),
        # Поля может не быть вовсе — например, если чек не передавали.
        receipt_registration=data.get("receipt_registration", ""),
        test=bool(data.get("test")),
        amount=float((data.get("amount") or {}).get("value") or 0),
    )


async def _call(method: str, path: str, payload: dict | None = None, key: str = "") -> dict:
    if not is_configured():
        raise YooKassaError("YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы")

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotence-Key"] = key

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method,
                f"{settings.yookassa_api_base_url}{path}",
                auth=(settings.yookassa_shop_id, settings.yookassa_secret_key),
                headers=headers,
                json=payload,
            )
    except httpx.RequestError as error:
        # Сеть оборвалась — ответа нет. Для POST это тот же случай, что и
        # 500: платёж мог создаться.
        raise YooKassaUnknown(f"ЮKassa не ответила: {error}") from error

    if response.status_code >= 500:
        raise YooKassaUnknown(f"ЮKassa не дала точного ответа — {_describe_failure(response)}")
    if response.status_code >= 400:
        raise YooKassaError(f"ЮKassa отказала на {path} — {_describe_failure(response)}")

    return response.json()


async def create_payment(
    *,
    order_key: str,
    items: list[dict],
    delivery_cost: float,
    delivery_label: str,
    email: str,
    phone: str,
    full_name: str,
    description: str,
) -> Payment:
    """Создать платёж вместе с данными чека.

    Сумма платежа считается из позиций чека, а не приходит отдельно: если
    посчитать её независимо, любое расхождение в копейку станет отказом
    ЮKassa — и уже на живом клиенте.
    """
    rows = receipt_items(items, delivery_cost, delivery_label)
    total = sum(float(row["amount"]["value"]) for row in rows)

    customer: dict = {}
    if full_name:
        customer["full_name"] = full_name[:256]
    if email:
        customer["email"] = email[:254]
    phone_digits = normalize_phone(phone)
    if phone_digits:
        customer["phone"] = phone_digits

    payload = {
        "amount": _money(total),
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": settings.yookassa_return_url or "https://vk.com",
        },
        "description": description[:128],
        "metadata": {"order": order_key},
        "receipt": {"customer": customer, "items": rows},
    }

    data = await _call("POST", "/payments", payload, key=idempotence_key(order_key))
    payment = _to_payment(data)
    logger.info(
        "ЮKassa: платёж %s на %s руб, статус %s, чек %s",
        payment.id, payment.amount, payment.status, payment.receipt_registration or "—",
    )
    return payment


async def get_payment(payment_id: str) -> Payment:
    """Состояние платежа. Им же проверяется подлинность уведомления."""
    return _to_payment(await _call("GET", f"/payments/{payment_id}"))


@dataclass(frozen=True)
class Refund:
    """Возврат денег клиенту. Его заводит менеджер в кабинете, не бот."""

    id: str
    payment_id: str
    status: str
    amount: float


async def account_info() -> dict:
    """Что за магазин отвечает на наши ключи.

    `GET /me` не создаёт ничего и отвечает разом на три вопроса: те ли
    ключи, тестовый магазин или боевой (`test`), включена ли фискализация.
    Нужен именно из контейнера: ключи у ноутбука и у ревизии — две разные
    копии, и проверка на ноутбуке про контейнер ничего не говорит.
    """
    return await _call("GET", "/me")


async def get_refund(refund_id: str) -> Refund:
    """Состояние возврата.

    Нужен, потому что уведомление `refund.succeeded` приносит объект
    возврата, а не платежа: спрашивать по его идентификатору `/payments/…`
    значит получить 404 и заставить ЮKassa повторять уведомление сутки.
    """
    data = await _call("GET", f"/refunds/{refund_id}")
    return Refund(
        id=data.get("id", ""),
        payment_id=data.get("payment_id", ""),
        status=data.get("status", ""),
        amount=float((data.get("amount") or {}).get("value") or 0),
    )
