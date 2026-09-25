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
import re
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
    # Кто и почему отменил платёж. От этого зависит, что сказать клиенту:
    # отказ банка — повод предложить другую карту, истёкший срок клиент уже
    # знает от нас, а отмену магазином объясняет менеджер сам.
    cancellation_party: str = ""
    cancellation_reason: str = ""


def is_configured() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


# Сколько цифр в телефоне по E.164: от восьми (короткие национальные) до
# пятнадцати. Российский номер — одиннадцать, и с него же начинается счёт.
_PHONE_MIN_DIGITS = 11
_PHONE_MAX_DIGITS = 15


def normalize_phone(raw: str) -> str:
    """Телефон цифрами, как требует ITU-T E.164: `79001234567`.

    Клиент пишет телефон как удобно — со скобками, плюсом, пробелами, через
    восьмёрку. ЮKassa принимает только цифры, и такой чек уйдёт в отказ.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


def phone_is_valid(raw: str) -> bool:
    """Годится ли телефон для чека.

    Проверяем у себя, а не узнаём из отказа ЮKassa: её ошибка приходит в
    момент выставления счёта — когда клиент уже сказал «оформляйте», — и
    выглядит для него как поломка вместо простого «уточните номер».
    """
    digits = normalize_phone(raw)
    return _PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def email_is_valid(raw: str) -> bool:
    """Похоже ли на почтовый адрес. Проверка грубая: отсеять опечатки вроде
    пропущенной собаки, а не разбирать RFC 5322 — это сделает ЮKassa."""
    return bool(_EMAIL.match((raw or "").strip()))


def receipt_customer(*, full_name: str, email: str, phone: str = "") -> dict:
    """Кому выписан чек.

    **Только почта.** В «Чеках от ЮKassa» `customer.email` обязателен, а чек
    доставляется исключительно письмом — так сказано в их документации и
    подтверждено поддержкой 25.09.2026 для каждого чека, включая закрывающий
    при вручении. Одно время мы клали телефон вместо отсутствующей почты:
    тестовый магазин (эмуляция сторонней кассы) это принимал, боевой — нет.

    `phone` остался в подписи, чтобы вызывающим не пришлось его убирать, но
    в чек не идёт.
    """
    customer: dict = {}
    if full_name:
        customer["full_name"] = full_name[:256]
    if email:
        customer["email"] = email.strip()[:254]
    return customer


def idempotence_key(order_key: str, attempt: int = 1) -> str:
    """Ключ, выведенный из заказа и номера попытки, а не случайный.

    Случайный ключ на повторе создал бы второй платёж — то есть списал бы с
    клиента дважды. Поэтому одна попытка обязана давать один ключ.

    А вот **новая** попытка обязана давать новый: первый счёт мог истечь или
    не пройти, и тогда нужен именно второй платёж. Номер заказа при этом не
    меняется — меняется только номер попытки.
    """
    suffix = f"#{attempt}" if attempt and attempt > 1 else ""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-order:{order_key}{suffix}"))


def _money(value: float) -> dict:
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


def receipt_items(items: list[dict], delivery_cost: float, delivery_label: str) -> list[dict]:
    """Позиции чека: товары плюс доставка отдельной строкой.

    Доставка — услуга, а не товар, и в чеке она обязана быть своей позицией:
    сумма платежа должна сходиться с суммой позиций чека до копейки.

    **`amount` позиции — цена за единицу** (тег 1079), а не стоимость строки:
    ЮKassa считает сумму чека как `quantity × amount`. Раньше сюда шла цена,
    умноженная на количество, и заказ из двух пачек одного сорта давал чек
    вдвое дороже платежа — счёт на такой заказ не выставлялся.
    """
    rows = []
    for item in items:
        quantity = item.get("quantity", 1)
        price = float(item.get("price", 0))
        rows.append(
            {
                "description": str(item.get("name", "Товар"))[:_MAX_ITEM_NAME],
                "quantity": quantity,
                "amount": _money(price),
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


def receipt_total(rows: list[dict]) -> float:
    """Сумма чека так, как её считает ЮKassa: цена × количество по позициям.

    Считаем в копейках: сложение рублей с плавающей точкой даёт хвосты
    вроде 1599.9999, и сумма платежа разойдётся с чеком на копейку.
    """
    kopecks = sum(
        round(float(row["amount"]["value"]) * 100) * row["quantity"] for row in rows
    )
    return round(kopecks / 100, 2)


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
    cancellation = data.get("cancellation_details") or {}
    return Payment(
        id=data.get("id", ""),
        status=data.get("status", ""),
        paid=bool(data.get("paid")),
        confirmation_url=(data.get("confirmation") or {}).get("confirmation_url", ""),
        # Поля может не быть вовсе — например, если чек не передавали.
        receipt_registration=data.get("receipt_registration", ""),
        test=bool(data.get("test")),
        amount=float((data.get("amount") or {}).get("value") or 0),
        cancellation_party=str(cancellation.get("party") or ""),
        cancellation_reason=str(cancellation.get("reason") or ""),
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
    attempt: int = 1,
) -> Payment:
    """Создать платёж вместе с данными чека.

    Сумма платежа считается из позиций чека, а не приходит отдельно: если
    посчитать её независимо, любое расхождение в копейку станет отказом
    ЮKassa — и уже на живом клиенте.
    """
    rows = receipt_items(items, delivery_cost, delivery_label)
    total = receipt_total(rows)

    customer = receipt_customer(full_name=full_name, email=email)
    if not customer.get("email"):
        raise YooKassaError(
            "в чеке нет почты — «Чеки от ЮKassa» без неё платёж не примут"
        )

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

    data = await _call(
        "POST", "/payments", payload, key=idempotence_key(order_key, attempt)
    )
    payment = _to_payment(data)
    logger.info(
        "ЮKassa: платёж %s на %s руб, статус %s, чек %s",
        payment.id, payment.amount, payment.status, payment.receipt_registration or "—",
    )
    return payment


async def cancel_payment(payment_id: str) -> Payment:
    """Отменить платёж, который клиент так и не оплатил.

    Работает не всегда: отменить можно платёж, ждущий подтверждения, а
    `pending` ЮKassa закрывает сама по истечении срока и на запрос отвечает
    отказом. Поэтому вызывающий обязан быть готов к `YooKassaError` — это
    штатный исход, а не поломка.

    Ключ идемпотентности выводим из идентификатора платежа: повторная
    попытка отменить то же самое не должна считаться новой операцией.
    """
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-cancel:{payment_id}"))
    data = await _call("POST", f"/payments/{payment_id}/cancel", {}, key=key)
    return _to_payment(data)


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
    return _to_refund(await _call("GET", f"/refunds/{refund_id}"))


def _to_refund(data: dict) -> Refund:
    return Refund(
        id=data.get("id", ""),
        payment_id=data.get("payment_id", ""),
        status=data.get("status", ""),
        amount=float((data.get("amount") or {}).get("value") or 0),
    )


async def create_refund(
    *,
    payment_id: str,
    amount: float,
    items: list[dict],
    delivery_cost: float,
    delivery_label: str,
    email: str,
    phone: str,
    full_name: str,
) -> Refund:
    """Вернуть деньги клиенту — с чеком возврата.

    Нужен там, где бот обязан вернуть сам: клиент заплатил по счёту дважды
    (например, по старой ссылке, которую ЮKassa всё ещё принимает). Держать
    у себя вторые деньги нельзя, а ждать менеджера — значит держать.

    Чек возврата обязателен на фискализированном магазине: иначе в ОФД
    останется приход без расхода. Позиции те же, что и в чеке платежа.

    Ключ идемпотентности выводим из платежа: повторное уведомление не
    должно возвращать деньги дважды.
    """
    rows = receipt_items(items, delivery_cost, delivery_label)
    customer = receipt_customer(full_name=full_name, email=email, phone=phone)
    payload = {
        "payment_id": payment_id,
        "amount": _money(amount),
        "receipt": {"customer": customer, "items": rows},
    }
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"tea-shop-refund:{payment_id}"))
    data = await _call("POST", "/refunds", payload, key=key)
    refund = _to_refund(data)
    logger.info(
        "ЮKassa: возврат %s по платежу %s на %s руб, статус %s",
        refund.id, payment_id, refund.amount, refund.status,
    )
    return refund


@dataclass(frozen=True)
class Receipt:
    """Чек, созданный отдельным запросом, — закрывающий при вручении."""

    id: str
    status: str
    payment_id: str


def _to_receipt(data: dict) -> Receipt:
    return Receipt(
        id=data.get("id", ""),
        status=data.get("status", ""),
        payment_id=data.get("payment_id", ""),
    )


async def create_receipt(payload: dict, key: str) -> Receipt:
    """Создать чек отдельным запросом (`POST /receipts`).

    Так формируется чек зачёта предоплаты при вручении: подтверждено
    поддержкой ЮKassa 25.09.2026, другого способа нет. Регистрирует его
    касса позже и асинхронно — статус приходит `pending`.
    """
    receipt = _to_receipt(await _call("POST", "/receipts", payload, key=key))
    logger.info(
        "ЮKassa: чек %s по платежу %s, статус %s",
        receipt.id, receipt.payment_id, receipt.status,
    )
    return receipt


async def get_receipt(receipt_id: str) -> Receipt:
    return _to_receipt(await _call("GET", f"/receipts/{receipt_id}"))
