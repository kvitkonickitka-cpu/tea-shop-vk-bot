from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Приложение 15 спецификации: истинный режим заказа. Первое слово — как
# посылка попадает от нас в СДЭК, второе — как она попадает к клиенту.
#
# Различать их обязательно. Самый дешёвый тариф почти всегда «склад-склад»,
# то есть клиент едет за посылкой сам. Если показать эту цену как стоимость
# доставки по адресу, магазин будет недобирать на каждом заказе.
MODE_DOOR_DOOR = 1
MODE_DOOR_WAREHOUSE = 2
MODE_WAREHOUSE_DOOR = 3
MODE_WAREHOUSE_WAREHOUSE = 4
MODE_DOOR_POSTAMAT = 6
MODE_WAREHOUSE_POSTAMAT = 7

# Курьер привозит клиенту по адресу.
TO_DOOR = (MODE_DOOR_DOOR, MODE_WAREHOUSE_DOOR)
# Клиент забирает сам в пункте выдачи.
TO_PICKUP = (MODE_DOOR_WAREHOUSE, MODE_WAREHOUSE_WAREHOUSE)
# Клиент забирает сам из постамата.
TO_POSTAMAT = (MODE_DOOR_POSTAMAT, MODE_WAREHOUSE_POSTAMAT)

_ORDER_TYPE_ONLINE_SHOP = 1
_TIMEOUT_SECONDS = 10

_token: str | None = None
_token_expires_at: float = 0.0


class CdekError(RuntimeError):
    """Ошибка на стороне СДЭК: отказ в авторизации, отказ расчёта, недоступность."""


@dataclass(frozen=True)
class Tariff:
    code: int
    name: str
    delivery_sum: float
    period_min: int
    period_max: int
    delivery_mode: int

    @property
    def period(self) -> str:
        if self.period_min == self.period_max:
            return f"{self.period_min} раб. дн."
        return f"{self.period_min}–{self.period_max} раб. дн."


def _describe_failure(response: httpx.Response) -> str:
    # СДЭК отвечает и кодом, и списком ошибок в теле, причём одно без другого
    # встречается. Собираем всё, что есть: иначе в логах остаётся голое «нет
    # тарифов», по которому причину не восстановить.
    parts = [f"HTTP {response.status_code}"]
    try:
        data = response.json()
    except ValueError:
        return f"{parts[0]}: {response.text[:300]}"

    for error in data.get("errors") or []:
        parts.append(f"{error.get('code', '?')}: {error.get('message', '')}")
    if len(parts) == 1:
        parts.append(str(data)[:300])
    return "; ".join(parts)


async def _get_access_token(client: httpx.AsyncClient) -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at:
        return _token

    response = await client.post(
        f"{settings.cdek_api_base_url}/v2/oauth/token",
        params={
            "grant_type": "client_credentials",
            "client_id": settings.cdek_client_id,
            "client_secret": settings.cdek_client_secret,
        },
    )
    if response.status_code >= 400:
        raise CdekError(f"СДЭК не выдал токен — {_describe_failure(response)}")

    data = response.json()
    if "access_token" not in data:
        raise CdekError(f"В ответе СДЭК нет токена: {str(data)[:300]}")

    _token = data["access_token"]
    # Минута запаса, чтобы не попасть в гонку с истечением прямо в запросе.
    _token_expires_at = time.time() + data.get("expires_in", 3600) - 60
    return _token


async def calculate_tariffs(
    to_address: str,
    weight_grams: int,
    *,
    delivery_point: str | None = None,
) -> list[Tariff]:
    """Все доступные тарифы до указанного адреса.

    Фильтровать по режиму доставки — задача вызывающего: см. `cheapest`.
    """
    if not settings.cdek_from_address:
        raise CdekError("CDEK_FROM_ADDRESS не задан — расчёт невозможен")

    payload: dict = {
        "type": _ORDER_TYPE_ONLINE_SHOP,
        "lang": "rus",
        "from_location": {"address": settings.cdek_from_address},
        "to_location": {"address": to_address},
        "packages": [{"weight": weight_grams}],
    }
    if delivery_point:
        # Код ПВЗ повышает точность расчёта сроков для режимов «до склада».
        payload["delivery_point"] = delivery_point

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        token = await _get_access_token(client)
        response = await client.post(
            f"{settings.cdek_api_base_url}/v2/calculator/tarifflist",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    if response.status_code >= 400:
        raise CdekError(f"Расчёт СДЭК не удался — {_describe_failure(response)}")

    data = response.json()
    if data.get("errors"):
        raise CdekError(f"Расчёт СДЭК не удался — {_describe_failure(response)}")
    for warning in data.get("warnings") or []:
        logger.warning("СДЭК предупреждает: %s", warning)

    tariffs = [
        Tariff(
            code=t["tariff_code"],
            name=t["tariff_name"],
            delivery_sum=float(t["delivery_sum"]),
            period_min=t["period_min"],
            period_max=t["period_max"],
            delivery_mode=t["delivery_mode"],
        )
        for t in data.get("tariff_codes") or []
    ]
    if not tariffs:
        raise CdekError(f"СДЭК не вернул ни одного тарифа — {_describe_failure(response)}")
    return tariffs


def cheapest(tariffs: list[Tariff], modes: tuple[int, ...]) -> Tariff | None:
    """Самый дешёвый тариф среди нужных режимов доставки."""
    suitable = [t for t in tariffs if t.delivery_mode in modes]
    return min(suitable, key=lambda t: t.delivery_sum) if suitable else None
