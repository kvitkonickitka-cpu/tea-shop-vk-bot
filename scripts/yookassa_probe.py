#!/usr/bin/env python3
"""Разведка ЮKassa: кто мы, проходит ли платёж, регистрируется ли чек.

Запускать руками. Ключи берутся из `.env`, в аргументах не передаются:
иначе секретный ключ осядет в истории команд.

    python scripts/yookassa_probe.py
        Кто мы: идентификатор магазина, тестовый он или боевой, включена ли
        фискализация, какие способы оплаты доступны. С этого стоит начинать:
        один запрос отвечает, те ли ключи лежат в .env.

    python scripts/yookassa_probe.py 1250 user@example.com
        Создать платёж на сумму с чеком на указанную почту. Печатает ссылку
        на оплату — её можно открыть и заплатить тестовой картой.

    python scripts/yookassa_probe.py --payment <идентификатор>
        Что стало с платежом и зарегистрировался ли чек.

В тестовом магазине платежи ненастоящие, деньги не двигаются.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.modules.payment import yookassa_client  # noqa: E402

_TIMEOUT_SECONDS = 30


def _auth() -> tuple[str, str]:
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        print("В .env нет YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY.")
        raise SystemExit(1)
    return settings.yookassa_shop_id, settings.yookassa_secret_key


def _show(title: str, data) -> None:
    print(f"--- {title} ---")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print()


async def _call(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        # Ключ идемпотентности обязателен для POST. Без него повтор запроса
        # после обрыва связи создал бы клиенту второй платёж.
        headers["Idempotence-Key"] = str(uuid.uuid4())

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method,
            f"{settings.yookassa_api_base_url}{path}",
            auth=_auth(),
            headers=headers,
            json=payload,
        )

    try:
        data = response.json()
    except ValueError:
        data = {"ответ не разобрался": response.text[:500]}

    if response.status_code >= 400:
        # 500 у ЮKassa не означает неудачу: она не смогла ответить точно за
        # 30 секунд. Что случилось с платежом на самом деле, показывает
        # отдельный запрос его состояния.
        print(f"HTTP {response.status_code}")
        if response.status_code >= 500:
            print("Это не значит, что платёж не прошёл. Запроси его состояние отдельно.")
    return data


async def whoami() -> None:
    data = await _call("GET", "/me")
    _show("Магазин", data)
    if data.get("test") is True:
        print("Магазин тестовый: платежи ненастоящие, деньги не двигаются.")
    elif data.get("test") is False:
        print("ВНИМАНИЕ: магазин БОЕВОЙ. Любой платёж здесь настоящий.")
    fiscal = data.get("fiscalization")
    print(f"Фискализация: {fiscal if fiscal else 'нет данных в ответе'}")


async def create_payment(amount: float, email: str, phone: str = "79001234567") -> None:
    """Создать платёж тем же кодом, которым это делает бот.

    Намеренно через `yookassa_client`, а не своим запросом: песочница
    проверяет формат, и проверять надо формат боевого кода, а не черновика
    из скрипта.
    """
    items = [{"name": "Те Гуань Инь, 100 г", "quantity": 1, "price": amount}]
    try:
        payment = await yookassa_client.create_payment(
            order_key=f"проверка-{uuid.uuid4().hex[:8]}",
            items=items,
            delivery_cost=0,
            delivery_label="",
            email=email,
            phone=phone,
            full_name="Иванов Иван Иванович",
            description="Проверка интеграции",
        )
    except yookassa_client.YooKassaUnknown as error:
        print(f"Ответа нет, платёж мог создаться: {error}")
        print("Проверь историю платежей в кабинете, прежде чем пробовать снова.")
        return
    except yookassa_client.YooKassaError as error:
        print(f"Отказ: {error}")
        return

    print(f"Платёж:     {payment.id}")
    print(f"Статус:     {payment.status}, оплачен: {payment.paid}")
    print(f"Сумма:      {payment.amount} руб")
    print(f"Контур:     {'тестовый' if payment.test else 'БОЕВОЙ'}")
    print(f"Чек:        {payment.receipt_registration or 'поле не пришло'}")
    customer = yookassa_client.receipt_customer(
        full_name="Иванов Иван Иванович", email=email
    )
    print(f"Кому чек:   {customer}")
    if payment.confirmation_url:
        print(f"\nСсылка на оплату: {payment.confirmation_url}")
        print("Открой её и заплати тестовой картой, потом посмотри состояние:")
        print(f"  scripts/yookassa_probe.py --payment {payment.id}")


async def payment_state(payment_id: str) -> None:
    data = await _call("GET", f"/payments/{payment_id}")
    _show("Платёж", data)
    print(f"Статус: {data.get('status')}, оплачен: {data.get('paid')}")
    print(f"Регистрация чека: {data.get('receipt_registration', 'поле не пришло')}")

    # В песочнице чек не формируется вовсе — там проверяется только формат
    # данных. Пустой список здесь не поломка, а ожидаемое поведение.
    receipts = await _call("GET", f"/receipts?payment_id={payment_id}")
    _show("Чеки по платежу", receipts)


async def main() -> int:
    args = sys.argv[1:]

    contour = "ТЕСТОВЫЙ" if settings.yookassa_shop_id.startswith("test") else ""
    print(f"Магазин: {settings.yookassa_shop_id or '— НЕ ЗАДАН'} {contour}")
    print(f"Хост:    {settings.yookassa_api_base_url}\n")

    if not args:
        await whoami()
    elif args[0] == "--payment" and len(args) > 1:
        await payment_state(args[1])
    elif len(args) >= 2:
        await create_payment(float(args[0]), args[1])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
