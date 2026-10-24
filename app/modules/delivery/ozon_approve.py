"""Автоподтверждение отправления Ozon после сборки (C8, флаг ozon_auto_approve).

Раньше `approve` бот не вызывал вовсе: подтверждение к отгрузке означает, что
посылку и правда повезут, и решал менеджер в кабинете. Теперь — когда на
странице сборки отсканированы все коды и заказ собран (`/pack/…/finish`):

- `off` — ничего, подтверждает менеджер (пункт «Подтвердить в Ozon» в утреннем списке);
- `allowlist` — **dry-run** для заказов тестовых аккаунтов: в лог уходит запрос,
  который был бы отправлен, у заказа — отметка `ozon_approve_dry_run_at`;
- `on` — для всех; настоящий запрос — только при `OZON_APPROVE_LIVE=true`,
  иначе тот же dry-run. Предохранитель отдельной настройкой: включить флаг
  командой мало, нужен осознанный шаг на ВМ.

Идемпотентно: отметка `ozon_approved_at` в заказе — повтор ничего не шлёт.
Ошибка Ozon — 🔴 в Ops, заказ остаётся в утреннем списке на ручное подтверждение.
TODO(ozon): тело `/v1/posting/approve` сверить с документацией до первого живого вызова.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core import flags
from app.core.config import settings
from app.core.database import get_session_factory

logger = logging.getLogger(__name__)

PATH = "/v1/posting/approve"


def request_for(posting: str) -> tuple[str, dict]:
    return PATH, {"posting_number": posting}


async def _mark(order_id: int, **fields) -> None:
    from app.modules.orders.models import Order

    async with get_session_factory()() as session:
        row = await session.get(Order, order_id)
        row.details = {**(row.details or {}), **fields}
        await session.commit()


async def after_packed(order_id: int) -> dict:
    """Заказ собран — подтвердить отправление Ozon (или показать, что было бы отправлено)."""
    from app.messages import funnel
    from app.modules.analytics import service as analytics
    from app.modules.orders import repository as orders_repository

    order = await orders_repository.by_id(order_id)
    if order is None or not order.ozon_posting:
        return {"skipped": "не Ozon"}
    details = order.details or {}
    if details.get("ozon_approved_at"):
        return {"skipped": "уже подтверждено"}
    mode = await flags.mode(flags.OZON_AUTO_APPROVE)
    if mode == flags.OFF:
        return {"skipped": "флаг выключен"}
    if mode == flags.ALLOWLIST and order.peer_id not in await analytics.test_peer_ids():
        return {"skipped": "allowlist: не тестовый заказ"}
    path, payload = request_for(order.ozon_posting)
    now = datetime.now(timezone.utc).isoformat()
    if not (mode == flags.ON and settings.ozon_approve_live):
        logger.warning("DRY-RUN Ozon approve для заказа %s: POST %s %s", order_id, path,
                       json.dumps(payload, ensure_ascii=False))
        await _mark(order_id, ozon_approve_dry_run_at=now)
        await funnel.record(order.peer_id, "ozon_approve_dry_run", order_id=order_id, source_=funnel.CODE)
        return {"dry_run": True, "request": {"path": path, "body": payload}}
    from app.modules.delivery import ozon_client

    try:
        await ozon_client.call(path, payload)
    except Exception as error:
        logger.exception("Ozon approve для заказа %s не прошёл", order_id)
        from app.messages import manager as manager_messages

        await manager_messages.notify(
            manager_messages.OPS_CONTROLS,
            f"🔴 <b>Заказ №{order_id}: Ozon не подтвердил отправление</b>\n{type(error).__name__}: "
            f"{str(error)[:200]}\nПодтвердите в кабинете Ozon — заказ в утреннем списке.",
            order_id=order_id, chat_id=settings.telegram_ops_chat_id or None,
        )
        return {"error": type(error).__name__}
    await _mark(order_id, ozon_approved_at=now)
    await funnel.record(order.peer_id, "ozon_approved", order_id=order_id, source_=funnel.CODE)
    return {"approved": True}
