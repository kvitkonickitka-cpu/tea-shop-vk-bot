#!/usr/bin/env python3
"""Что стало с заказом в СДЭКе.

`POST /v2/orders` отвечает `202 Accepted` — это «заявку взяли в обработку»,
а не «заказ создан». Проверку СДЭК делает асинхронно, и если она не прошла,
заказа в личном кабинете не будет, а причина останется только здесь.

    python scripts/cdek_order_state.py 216dc15e-2801-423e-bc69-49949b6f1264

Ключи берутся из .env или переменных окружения, в аргументах не передаются.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.modules.delivery import cdek_client  # noqa: E402

_STATE_MEANING = {
    "ACCEPTED": "принята в обработку",
    "WAITING": "ждёт выполнения другого запроса",
    "SUCCESSFUL": "выполнена — заказ создан",
    "INVALID": "ОТКЛОНЕНА — заказа не будет",
}


async def main() -> int:
    if len(sys.argv) < 2:
        print("Нужен uuid заказа. Он есть в логах: «заведён в СДЭКе: uuid=...»")
        return 2

    uuid = sys.argv[1]
    contour = "ПЕСОЧНИЦА" if "edu" in settings.cdek_api_base_url else "БОЕВОЙ КОНТУР"
    print(f"Контур: {contour} ({settings.cdek_api_base_url})")
    print(f"Заказ:  {uuid}\n")

    try:
        data = await cdek_client.order_state(uuid)
    except cdek_client.CdekError as error:
        print(f"Не получилось: {error}")
        return 1

    entity = data.get("entity") or {}
    number = entity.get("cdek_number")
    print(f"Наш номер:  {entity.get('number') or '—'}")
    print(f"Номер СДЭК: {number or '— не присвоен'}")
    if entity.get("delivery_point"):
        print(f"Пункт выдачи: {entity['delivery_point']}")
    if entity.get("shipment_point"):
        print(f"Отделение отправки: {entity['shipment_point']}")

    print("\nЗаявки по заказу:")
    failed = False
    for request in data.get("requests") or []:
        state = request.get("state", "?")
        meaning = _STATE_MEANING.get(state, "")
        print(f"  {request.get('type', '?'):8} {state:12} {meaning}")
        for error in request.get("errors") or []:
            failed = True
            print(f"      ОШИБКА: {cdek_client.describe_request_error(error)}")
        for warning in request.get("warnings") or []:
            print(f"      предупреждение: {warning.get('code')}: {warning.get('message')}")

    statuses = entity.get("statuses") or []
    if statuses:
        print("\nСтатусы заказа:")
        for status in statuses:
            print(f"  {status.get('date_time', '')} {status.get('name', '')}")

    if failed:
        print("\nЗаказ отклонён — текст ошибки выше говорит, что именно не понравилось.")
    elif not number:
        print("\nНомер не присвоен: либо ещё обрабатывается, либо отклонён без текста.")
        print("Повтори запуск через минуту.")
    else:
        print("\nЗаказ создан — он должен быть виден в личном кабинете.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
