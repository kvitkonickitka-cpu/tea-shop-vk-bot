from __future__ import annotations

import time

import httpx

from app.core.config import settings

_token: str | None = None
_token_expires_at: float = 0.0


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
    data = response.json()
    if "access_token" not in data:
        raise RuntimeError(f"CDEK auth error: {data}")

    _token = data["access_token"]
    _token_expires_at = time.time() + data.get("expires_in", 3600) - 60
    return _token


async def calculate_cheapest_tariff(to_address: str, weight_grams: int) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        token = await _get_access_token(client)
        response = await client.post(
            f"{settings.cdek_api_base_url}/v2/calculator/tarifflist",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "from_location": {"address": settings.cdek_from_address},
                "to_location": {"address": to_address},
                "packages": [{"weight": weight_grams}],
            },
        )
        data = response.json()
        tariffs = data.get("tariff_codes", [])
        if not tariffs:
            raise RuntimeError(f"CDEK returned no tariffs: {data}")

        return min(tariffs, key=lambda t: t["delivery_sum"])
