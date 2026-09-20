#!/usr/bin/env python3
"""Проверка расчёта СДЭК на живом контуре.

Запускать руками — из окружения разработки контур СДЭК недоступен, а сверить
цифры с личным кабинетом надо. Ключи берутся из переменных окружения, в
аргументах не передаются: иначе они осядут в истории команд.

    export CDEK_CLIENT_ID=...
    export CDEK_CLIENT_SECRET=...
    export CDEK_FROM_ADDRESS="Москва, ул. Ленина, 1"
    python scripts/cdek_check.py "Санкт-Петербург, Невский пр-т, 1" 400

Второй аргумент — вес в граммах, по умолчанию 200.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.modules.delivery import cdek_client  # noqa: E402

_MODE_LABELS = {
    1: "дверь → дверь",
    2: "дверь → склад",
    3: "склад → дверь",
    4: "склад → склад",
    6: "дверь → постамат",
    7: "склад → постамат",
    # Отправка из постамата: магазину не подходит, но СДЭК эти тарифы
    # всё равно возвращает — пусть в выводе будут подписаны, а не «режим 8».
    8: "постамат → дверь",
    9: "постамат → склад",
    10: "постамат → постамат",
}


async def main() -> int:
    address = sys.argv[1] if len(sys.argv) > 1 else "Санкт-Петербург, Невский проспект, 1"
    weight = int(sys.argv[2]) if len(sys.argv) > 2 else 200

    contour = "ПЕСОЧНИЦА" if "edu" in settings.cdek_api_base_url else "БОЕВОЙ КОНТУР"
    print(f"Контур:  {contour}  ({settings.cdek_api_base_url})")
    print(f"Откуда:  {settings.cdek_from_address or '— НЕ ЗАДАН, расчёт не пойдёт'}")
    print(f"Куда:    {address}")
    print(f"Вес:     {weight} г\n")

    try:
        tariffs = await cdek_client.calculate_tariffs(address, weight)
    except cdek_client.CdekError as error:
        print(f"Не получилось: {error}")
        return 1

    by_mode: dict[int, list] = {}
    for tariff in tariffs:
        by_mode.setdefault(tariff.delivery_mode, []).append(tariff)

    for mode in sorted(by_mode):
        print(f"--- {_MODE_LABELS.get(mode, f'режим {mode}')} ---")
        for tariff in sorted(by_mode[mode], key=lambda t: t.delivery_sum):
            print(f"  {tariff.delivery_sum:>9.2f} руб   {tariff.period:<14} "
                  f"[{tariff.code}] {tariff.name}")
        print()

    door = cdek_client.cheapest(tariffs, cdek_client.TO_DOOR)
    pickup = cdek_client.cheapest(tariffs, cdek_client.TO_PICKUP)

    print("Что выберет бот:")
    print(f"  до двери клиента:  {f'{door.delivery_sum:.2f} руб, {door.period}, {door.name}' if door else 'нет подходящего тарифа'}")
    print(f"  до пункта выдачи:  {f'{pickup.delivery_sum:.2f} руб, {pickup.period}, {pickup.name}' if pickup else 'нет подходящего тарифа'}")

    if door and pickup and door.delivery_sum != pickup.delivery_sum:
        diff = door.delivery_sum - pickup.delivery_sum
        print(f"\nРазница между «до двери» и «в пункт выдачи»: {diff:+.2f} руб.")
        print("Прежний код взял бы меньшую цифру независимо от того, куда везём.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
